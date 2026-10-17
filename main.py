#!/usr/bin/env python3
"""
main.py
=======

CLI entry point for the Even Realities G1 developer backend.

Owns the BLE connection and the interactive session. The LLM pipeline lives in
``pipeline.py``, reaction history in ``memory.py``, and card lifecycle /
drill-down in ``session.py`` — this module wires them together and does not
reimplement any of their logic.

    terminal query / TouchBar tap
        |
        v
    Session.ask() / Session.more()
        |
        +-> pipeline.run_query()   stage 1 reason -> stage 2 compress
        |       |
        |       v
        |   EvenG1Controller.send_text_packet()   0x4E -> both arms
        |
        +-> memory.FeedbackStore     cards + reactions, persisted locally
        |
        +-> plugins                  optional role overrides / refinement

Every stage failure is contained: the display always receives a payload and
the BLE connection is never torn down by an upstream error.

Examples::

    python main.py --selftest                # offline checks, no radio, no API key
    python main.py --llm-check               # probe the LLM path only
    python main.py --provider ollama         # local model, no key needed
    python main.py                            # interactive session
    python main.py --ask "why is the sky blue"
    python main.py --one-shot --text "hello"  # literal text, no LLM
    python main.py --scan-only               # list what advertises
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time

from bleak import BleakBackend
from bleak.backends import get_default_backend
from bleak.exc import BleakError

from g1_driver import EvenG1Controller, hexdump
from memory import FeedbackStore
from pipeline import (
    DEFAULT_MAX_DISPLAY_CHARS,
    DEFAULT_MAX_DISPLAY_WORDS,
    DISPLAY_LINES,
    LLMConfig,
    LLMStageError,
    LLMUnavailable,
    PROVIDERS,
    build_llm_config,
    condense_for_glasses,
    describe_llm_config,
    enforce_display_budget,
    ensure_models,
    fits_display,
    generate_primary_response,
    list_available_models,
    make_client,
    run_query,
    strip_display_noise,
    word_count,
)
from session import (
    DEFAULT_TOUCH_ACTIONS,
    UNMAPPED_TOUCH_EVENTS,
    Session,
)

log = logging.getLogger("g1")

DEFAULT_TEST_TEXT = "Terminal connection established. System online."
DEFAULT_HOLD_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Bluetooth helpers
# ---------------------------------------------------------------------------

def preflight() -> None:
    """
    Fail fast with an actionable message if the host cannot do BLE at all.

    Runs before the first scan so the user gets a Bluetooth problem instead of
    a 12-second timeout and an empty result.
    """
    try:
        backend = get_default_backend()
    except Exception as exc:
        print(f"\n  ! No usable BLE backend on this host: {exc}", file=sys.stderr)
        print("    bleak could not load a platform backend.", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"BLE backend: {backend}")

    if backend == BleakBackend.BLUEZ_DBUS:
        if not sys.platform.startswith("linux"):
            return
        try:
            import dbus_fast  # noqa: F401  (import check only)
        except ImportError:
            print(
                "\n  ! dbus-fast is missing — bleak cannot talk to BlueZ.\n"
                "    fix: pip install -r requirements.txt",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(
            "  tip: ensure the adapter is powered on and your user is in the\n"
            "       'bluetooth' group (sudo usermod -aG bluetooth $USER)."
        )
    elif backend in (BleakBackend.WIN_RT, BleakBackend.CORE_BLUETOOTH):
        print("  MTU is negotiated automatically on this backend.")
    else:
        print(f"  ! Unrecognised BLE backend {backend!r}.", file=sys.stderr)


def humanise_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds >= 60:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds:.0f}s"


def summarise_packets(controller: EvenG1Controller) -> None:
    """Print a per-command tally of everything received this session."""
    counts: dict[str, int] = {}
    for _side, data in controller._packet_log:
        if not data:
            continue
        key = f"0x{data[0]:02x}"
        if data[0] == 0xF5 and len(data) >= 2:
            key += f" 0x{data[1]:02x}"
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        print("No packets were received.")
        return

    print("\nPacket tally (command byte -> count):")
    for key, count in sorted(counts.items()):
        print(f"  {key:<8} {count}")

    if all(side == "L" for side, _ in controller._packet_log):
        print(
            "\n  note: only the LEFT arm sent packets. On BlueZ the MTU may "
            "have stayed at 23; try --verbose and check 'MTU=' in the log."
        )


async def monitor_touchbars(controller: EvenG1Controller, hold: float) -> int:
    """Stay connected for ``hold`` seconds, counting TouchBar packets."""
    deadline = time.monotonic() + hold
    print(
        f"\nConnected. Monitoring TouchBars for {humanise_duration(hold)} "
        f"— tap, double-tap or hold either bar."
    )
    print("Press Ctrl+C to finish early.\n")

    start = time.monotonic()
    last_report = start

    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if now - last_report >= 10.0:
            last_report = now
            print(
                f"  ... {humanise_duration(now - start)} elapsed, "
                f"{len(controller._packet_log)} packet(s) received"
            )
        await asyncio.sleep(0.1)

    print(
        f"\nMonitor window closed after {humanise_duration(time.monotonic() - start)}: "
        f"{len(controller._packet_log)} packet(s) received."
    )
    return len(controller._packet_log)


# ---------------------------------------------------------------------------
# Wiring: controller -> session
# ---------------------------------------------------------------------------

def build_sender(
    controller: EvenG1Controller,
    args: argparse.Namespace,
):
    """
    Build the async ``send`` callback the session renders through.

    An empty string means "clear the display", which is the 0x18 command
    rather than an empty 0x4E packet.
    """

    async def send(text: str) -> None:
        if text:
            await controller.send_text_packet(
                text,
                chars_per_line=args.chars_per_line,
                clear_first=args.clear,
                require_ack=args.require_ack,
                response=args.response_mode,
            )
        else:
            await controller.send_raw(bytes([0x18]))  # clear screen

    return send


def build_session(
    controller: EvenG1Controller,
    args: argparse.Namespace,
    config: LLMConfig,
) -> Session:
    """
    Assemble a Session and connect it to the live TouchBar stream.

    TouchBar actions are scheduled off the notification path, so an LLM call
    never blocks the BLE callback or the heartbeat.
    """
    store = FeedbackStore(args.store)
    session = Session(
        send=build_sender(controller, args),
        store=store,
        config=config,
    )

    def on_packet(side: str, data: bytes) -> None:
        session.handle_touch(side, data)

    controller.add_listener(on_packet)
    return session


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

REPL_HELP = """
Commands:
  <any text>        run the two-stage pipeline and mirror it to the glasses
  :more             drill down on the current card (same as a single tap)
  :dismiss          back out one level / clear (same as a double tap)
  :confirm          mark the current summary as good
  :star             mark the current topic as important
  :text <literal>   send literal text, bypassing the LLM
  :stage1 <query>   run only stage 1 and print the full answer (no BLE write)
  :battery          query both arms for battery level
  :stats            packet tally plus session counters
  :prefs            what the feedback loop has learned so far
  :config           show the active LLM configuration
  :help             this message
  :quit             disconnect and exit

TouchBar packets print as they arrive, interleaved with the prompt.
""".strip()


async def prompt_user(message: str) -> str:
    """
    Read one line without blocking the event loop.

    ``input()`` runs on a worker thread so the loop keeps servicing the
    heartbeat and incoming TouchBar notifications while the user thinks.
    Calling it directly would freeze the session, and a frozen loop stops the
    heartbeat — the G1 drops the link after ~32s idle.
    """
    return await asyncio.to_thread(input, message)


async def repl(
    controller: EvenG1Controller,
    args: argparse.Namespace,
    config: LLMConfig,
) -> Session:
    """Interactive query loop. The BLE connection stays open throughout."""
    session = build_session(controller, args, config)

    print("\n" + "=" * 68)
    print("G1 interactive session — type a question, or :help")
    print(f"LLM: {describe_llm_config(config)}")
    print(f"Display budget: {config.max_display_chars} chars "
          f"(~{config.max_display_words} words, {DISPLAY_LINES} lines)")
    if config.max_display_chars:
        try:
            client = make_client(config)
            await ensure_models(config, client)
            session.config = config
        except LLMUnavailable as exc:
            print(f"  ! LLM not ready: {exc}")
            print("    The glasses stay connected; :text still works.")
    print("=" * 68)

    while True:
        try:
            line = await prompt_user("\ng1> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        line = (line or "").strip()
        if not line:
            continue

        # -- commands ------------------------------------------------------
        if line.startswith(":"):
            command, _, rest = line[1:].partition(" ")
            command = command.lower()
            rest = rest.strip()

            if command in ("quit", "q", "exit"):
                print("bye.")
                break
            if command == "help":
                print(REPL_HELP)
                continue
            if command == "config":
                print(describe_llm_config(session.config))
                continue
            if command == "stats":
                summarise_packets(controller)
                print(f"  session: {session.summary()}")
                continue
            if command == "prefs":
                learned = session.preferences()
                print(learned if learned else
                      "  nothing learned yet — a few reactions are needed.")
                continue
            if command == "battery":
                try:
                    print(f"\nBattery: {await controller.query_battery()}")
                except BleakError as exc:
                    print(f"\nBLE error: {exc}")
                continue
            if command == "more":
                try:
                    card = await session.more()
                    if card is None:
                        print("  nothing to expand.")
                    else:
                        print(f"\n>>> waveguide: {card.card.summary}")
                except (LLMUnavailable, LLMStageError) as exc:
                    print(f"\nexpand failed: {exc}")
                continue
            if command == "dismiss":
                card = await session.dismiss()
                print("  dismissed." if card is None
                      else f"\n>>> back to: {card.card.summary}")
                continue
            if command in ("confirm", "star"):
                ok = (await session.confirm() if command == "confirm"
                      else await session.star())
                print("  recorded." if ok else "  no card showing.")
                continue
            if command == "stage1":
                if not rest:
                    print("  usage: :stage1 <query>")
                    continue
                print("\n[1/2] thinking ...", flush=True)
                try:
                    client = make_client(session.config)
                    session.config = await ensure_models(session.config, client)
                    answer = await generate_primary_response(
                        rest, client=client, config=session.config
                    )
                    print(f"\n--- stage 1 ({word_count(answer)} words) ---\n"
                          f"{answer}\n")
                except LLMUnavailable as exc:
                    print(f"\nLLM not configured: {exc}")
                except LLMStageError as exc:
                    print(f"\nstage 1 failed: {exc}")
                continue
            if command == "text":
                if not rest:
                    print("  usage: :text <literal>")
                    continue
                try:
                    await build_sender(controller, args)(rest)
                    print(f"  sent {word_count(rest)} words")
                except BleakError as exc:
                    print(f"  BLE error: {exc}")
                continue

            print(f"  unknown command ':{command}' — try :help")
            continue

        # -- a real question: the full pipeline ------------------------------
        try:
            card = await session.ask(line)
            print(f"\n>>> waveguide: {card.card.summary}")
            print(f"    ({word_count(card.card.summary)} words, "
                  f"{fits_display(card.card.summary, chars_per_line=args.chars_per_line)} "
                  f"lines) — tap for more, double-tap to dismiss")
        except LLMUnavailable as exc:
            print(f"\nLLM unavailable: {exc}")
            print("    Glasses stay connected. Use :text to send literal text.")
        except LLMStageError as exc:
            print(f"\nstage error: {exc}")
        except BleakError as exc:
            print(f"\nBLE error: {exc}")
            print("    Session still up — :quit and reconnect if it persists.")

    return session


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    # -- policy: contested questions, shown as switches --------------------
    if args.policy_axes:
        from policy import describe_axes

        print(describe_axes())
        return 0

    if args.set_policy:
        from policy import save_overrides

        pairs = [p for p in args.set_policy if "=" in p]
        if not pairs:
            print("expected axis=option, e.g. g1_model=on", file=sys.stderr)
            return 2
        overrides = {}
        for pair in pairs:
            axis_id, _, option = pair.partition("=")
            overrides[axis_id.strip()] = option.strip()
        try:
            path = save_overrides(overrides)
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        print(f"saved overrides -> {path}")

    if args.policy:
        from policy import load as load_policy

        pol = load_policy()
        print("Resolved policy (contested decisions, each with its deciding "
              "metric):\n")
        print(pol.report())
        return 0

    # -- discovery-only commands, before touching the radio ----------------
    if args.list_providers:
        print("Provider presets (all speak the OpenAI Chat Completions API):\n")
        for name in sorted(PROVIDERS):
            p = PROVIDERS[name]
            key = " (no key needed)" if p.local else f" key: {p.key_env[0]}"
            url = p.base_url or "default OpenAI endpoint"
            print(f"  {name:<11} {url}{key}")
            primary = p.default_primary or "(auto-discovered)"
            comp = p.default_compressor or "(auto-discovered)"
            print(f"  {'':<11} stage1={primary}  stage2={comp}")
        print(
            "\nAny other OpenAI-compatible server works via:\n"
            "  export OPENAI_BASE_URL=http://host:port/v1\n"
            "  export OPENAI_API_KEY=anything\n"
            "  export OPENAI_MODEL=<model-name>\n"
            "\nOverrides always win over presets: OPENAI_BASE_URL,\n"
            "G1_PRIMARY_MODEL, G1_COMPRESSOR_MODEL, G1_LLM_TIMEOUT,\n"
            "G1_LLM_RETRIES, G1_MAX_DISPLAY_WORDS, G1_MAX_DISPLAY_CHARS."
        )
        return 0

    if args.list_models:
        config = build_llm_config(args.provider)
        try:
            models = await list_available_models(config)
        except LLMUnavailable as exc:
            sys.stdout.flush()
            print(f"\n{exc}", file=sys.stderr)
            return 2
        print(f"Models served by {config.target}:")
        for name in models or ["(none reported)"]:
            print(f"  {name}")
        return 0

    if args.llm_check:
        config = build_llm_config(args.provider)
        if args.max_display_words:
            config.max_display_words = args.max_display_words
        if args.max_display_chars:
            config.max_display_chars = args.max_display_chars
        print(f"LLM config: {describe_llm_config(config)}")
        try:
            client = make_client(config)
        except LLMUnavailable as exc:
            sys.stdout.flush()
            print(f"\nLLM not configured:\n{exc}", file=sys.stderr)
            return 2
        config = await ensure_models(config, client)
        print("\nprobing stage 2 with a one-word test input ...")
        try:
            probe = await condense_for_glasses(
                "The capital of France is Paris.", client=client, config=config
            )
        except LLMStageError as exc:
            sys.stdout.flush()
            print(f"\nprobe failed: {exc}", file=sys.stderr)
            return 1
        print(f"probe ok -> {probe!r} "
              f"({word_count(probe)} words, {len(probe)} chars, "
              f"{fits_display(probe)} lines)")
        return 0

    preflight()

    config = build_llm_config(args.provider)
    if args.max_display_words:
        config.max_display_words = args.max_display_words
    if args.max_display_chars:
        config.max_display_chars = args.max_display_chars
    if not args.no_text:
        print(f"LLM: {describe_llm_config(config)}")
    print(f"Display budget: {config.max_display_chars} chars / "
          f"{config.max_display_words} words / {DISPLAY_LINES} lines")

    controller = EvenG1Controller(
        scan_timeout=args.scan_timeout,
        mtu=args.mtu,
        echo_packets=not args.quiet,
    )

    print(f"\nScanning for G1 arms for {args.scan_timeout:.0f}s ...")
    exit_code = 0
    session: Session | None = None

    try:
        if args.scan_only:
            found = await controller.discover(
                timeout=args.scan_timeout,
                require_both_arms=not args.allow_single_arm,
            )
            print(f"\n{found.describe()}  (in {found.elapsed:.1f}s)")
            for identity in found.identities.values():
                print(f"  {identity.label}")
            for identity in found.nus_only:
                print(f"  [nus-only, not claimed as G1] {identity.label}")
            if not found.channels:
                print("\nNothing matched. Confirm the glasses are powered on "
                      "and not bonded to the Even Realities app.")
                return 1
            return 0

        print("Connecting ...")
        result = await controller.connect(
            channel=args.channel,
            allow_single_arm=args.allow_single_arm,
        )
        print(f"  {result.describe()}")

        for arm in controller.arms:
            print(f"  {arm.identity.side} arm {arm.identity.address} "
                  f"connected, MTU={arm.mtu}")

        if len(controller.arms) < 2:
            print("\n  ! Connected to only one arm. Text will appear on that "
                  "side only.\n    Re-run with the other arm awake and unpaired.")

        if args.dump_services:
            controller.dump_services()
        if args.battery:
            print(f"\nBattery: {await controller.query_battery()}")

        # -- one query -------------------------------------------------------
        if args.ask:
            print(f"\nQuery: {args.ask}")
            try:
                outcome = await run_query(
                    args.ask, build_sender(controller, args), config=config
                )
                print(f"\n>>> waveguide: {outcome.display_text}")
                print(f"    ({word_count(outcome.display_text)} words, "
                      f"{fits_display(outcome.display_text)} lines)")
            except LLMUnavailable as exc:
                sys.stdout.flush()
                print(f"\nLLM unavailable: {exc}", file=sys.stderr)
                exit_code = 2
            return exit_code

        # -- literal text ----------------------------------------------------
        if args.text and not args.no_text:
            print(f"\nSending: {args.text!r}")
            packets = await controller.send_text_packet(
                args.text,
                chars_per_line=args.chars_per_line,
                clear_first=args.clear,
                require_ack=args.require_ack,
                response=args.response_mode,
            )
            print(f"  {len(packets)} packet(s) written")
            if args.verbose:
                for index, packet in enumerate(packets):
                    print(f"    [{index}] {hexdump(packet)}")

        # -- interactive -----------------------------------------------------
        if args.interactive or (sys.stdin.isatty() and not args.one_shot):
            session = await repl(controller, args, config)
            if session.stats.turns or session.stats.expansions:
                print(f"\n  {session.summary()}")
        elif args.hold > 0:
            await monitor_touchbars(controller, args.hold)
            summarise_packets(controller)

    except BleakError as exc:
        sys.stdout.flush()
        print(f"\nBLE error: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted — shutting down")
    except asyncio.CancelledError:
        print("\ncancelled — shutting down")
    finally:
        # Always release the adapter so the next run can reconnect at once.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.disconnect(), timeout=10.0)

    if exit_code == 0 and controller._packet_log and session is None:
        summarise_packets(controller)

    print("done.")
    return exit_code


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Scan for Even Realities G1 glasses, connect, and mirror a "
            "two-stage LLM pipeline onto the waveguide."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py                        # interactive session\n"
            "  python main.py --provider ollama      # local model, no key\n"
            "  python main.py --llm-check            # probe the LLM only\n"
            "  python main.py --ask 'why is the sky blue'\n"
            "  python main.py --one-shot --text 'hello'\n"
            "  python main.py --prefs                # what the loop learned\n"
        ),
    )

    # -- pipeline ---------------------------------------------------------
    parser.add_argument(
        "--ask", metavar="QUERY",
        help="run one pipeline query (stage 1 -> stage 2 -> BLE) and exit",
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true",
        help="force the interactive REPL (default when stdin is a TTY)",
    )
    parser.add_argument(
        "--one-shot", action="store_true",
        help="never enter the REPL; send --text and exit",
    )
    parser.add_argument(
        "--provider", metavar="NAME", choices=sorted(PROVIDERS),
        help="provider preset: " + ", ".join(sorted(PROVIDERS)),
    )
    parser.add_argument(
        "--list-providers", action="store_true",
        help="print the provider presets and exit",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="ask the endpoint which models it serves, then exit",
    )
    parser.add_argument(
        "--llm-check", action="store_true",
        help="validate LLM configuration and probe stage 2, then exit",
    )
    parser.add_argument(
        "--max-display-words", type=int, default=DEFAULT_MAX_DISPLAY_WORDS,
        metavar="N", help=f"word ceiling (default: {DEFAULT_MAX_DISPLAY_WORDS})",
    )
    parser.add_argument(
        "--max-display-chars", type=int, default=DEFAULT_MAX_DISPLAY_CHARS,
        metavar="N",
        help=f"character ceiling, the one that maps to the screen "
             f"(default: {DEFAULT_MAX_DISPLAY_CHARS} = {DISPLAY_LINES} lines "
             f"x 24 chars)",
    )

    # -- payload ----------------------------------------------------------
    parser.add_argument(
        "--text", metavar="STR", nargs="?", const=DEFAULT_TEST_TEXT,
        default=DEFAULT_TEST_TEXT,
        help=f"literal text, bypassing the LLM (default: {DEFAULT_TEST_TEXT!r})",
    )
    parser.add_argument(
        "--no-text", action="store_true",
        help="do not send the default literal text on startup",
    )

    # -- session ----------------------------------------------------------
    parser.add_argument(
        "--store", metavar="PATH",
        help="feedback database path "
             "(default: ~/.local/share/g1-ble-backend/feedback.db)",
    )
    parser.add_argument(
        "--policy", action="store_true",
        help="show the resolved policy for contested decisions, and exit",
    )
    parser.add_argument(
        "--policy-axes", action="store_true",
        help="list every policy axis, its options and its deciding metric",
    )
    parser.add_argument(
        "--set-policy", metavar="AXIS=OPTION", action="append",
        help="pin a policy axis manually, e.g. --set-policy g1_model=on "
             "(persists to the policy file)",
    )

    # -- connection -------------------------------------------------------
    parser.add_argument(
        "--hold", type=float, default=DEFAULT_HOLD_SECONDS, metavar="SEC",
        help=f"seconds to monitor TouchBars non-interactively "
             f"(default: {DEFAULT_HOLD_SECONDS:.0f})",
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=12.0, metavar="SEC",
        help="seconds to scan for the glasses (default: 12)",
    )
    parser.add_argument(
        "--channel", metavar="XX",
        help="pin a specific G1 channel (e.g. 07) instead of auto-selecting",
    )
    parser.add_argument(
        "--chars-per-line", type=int, default=24, metavar="N",
        help="characters per display line (default: 24)",
    )
    parser.add_argument(
        "--mtu", type=int, default=251, metavar="N",
        help="preferred ATT MTU (default: 251, as used by the official app)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="send the 0x18 clear-screen command before the text",
    )
    parser.add_argument(
        "--require-ack", action="store_true",
        help="wait for the glasses to acknowledge the text packets",
    )
    parser.add_argument(
        "--response", choices=("write", "no-response"), default=None,
        help="force a BLE write mode instead of choosing from characteristic "
             "properties (default: auto)",
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="scan and print what is advertising, then exit",
    )
    parser.add_argument(
        "--allow-single-arm", action="store_true",
        help="accept a channel with only one arm present",
    )
    parser.add_argument(
        "--dump-services", action="store_true",
        help="print the GATT service/characteristic table after connecting",
    )
    parser.add_argument(
        "--battery", action="store_true",
        help="query both arms for battery level",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="run offline packet + pipeline checks (no radio, no API key)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress per-packet RX lines",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="debug logging, including BLE writes and MTU negotiation",
    )
    return parser


# ---------------------------------------------------------------------------
# Offline self-test
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, replies: list[str], *, fail_at: int | None = None) -> None:
        self.replies = replies
        self.fail_at = fail_at
        self.calls: list[dict] = []

    async def create(self, *, model, messages, **kwargs):
        index = len(self.calls)
        self.calls.append({"model": model, "messages": messages, **kwargs})
        if self.fail_at is not None and index == self.fail_at:
            raise RuntimeError("simulated upstream failure")
        return _FakeResponse(self.replies[index] if index < len(self.replies) else "")


class _FakeClient:
    def __init__(self, replies: list[str], *, fail_at: int | None = None) -> None:
        self.completions = _FakeCompletions(replies, fail_at=fail_at)
        self.chat = type("_Chat", (), {"completions": self.completions})()


def selftest() -> int:
    """Verify the BLE packet format and the LLM pipeline offline."""
    import asyncio

    from g1_driver import selftest as ble_selftest

    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual == expected:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}\n         expected {expected!r}\n"
                  f"         actual   {actual!r}")
            failures.append(label)

    print("BLE packet format")
    ble_status = ble_selftest()

    print("\ndisplay post-processing")
    check("strips markdown", strip_display_noise("**bold** text"), "bold text")
    check("strips bullets", strip_display_noise("- first\n- second"), "first second")
    check("strips emoji", strip_display_noise("hello \U0001F600 world"), "hello world")
    check("strips stacked preamble",
          strip_display_noise("Sure, here's the thing"), "the thing")
    check("strips wrapping quotes", strip_display_noise('"quoted"'), "quoted")

    print("\ndisplay budget (character ceiling is what fits the screen)")
    long_text = " ".join(f"word{i}" for i in range(60))
    trimmed = enforce_display_budget(long_text)
    check("respects character ceiling", len(trimmed) <= DEFAULT_MAX_DISPLAY_CHARS, True)
    check("respects word ceiling", word_count(trimmed) <= DEFAULT_MAX_DISPLAY_WORDS, True)
    check("marks truncation", trimmed.endswith("…"), True)
    check("short text untouched", enforce_display_budget("hi there"), "hi there")
    check("empty input", enforce_display_budget(""), "")

    # The bug this fixes: 25 words used to render as 9 lines on a 5-line screen.
    twentyfive = " ".join(["example"] * 25)
    check("25 words now fits the screen",
          fits_display(enforce_display_budget(twentyfive, max_words=25)) <= DISPLAY_LINES,
          True)
    check("line count is derived from characters",
          fits_display("a" * 120), 5)
    check("long single word still sent",
          len(enforce_display_budget("x" * 400)) > 0, True)

    print("\npage fields are screens, not BLE fragments")
    from g1_driver import EvenG1Controller

    controller = EvenG1Controller()
    # MTU 23 fragments a card; the glasses must still see ONE screen.
    fragmented = controller.build_text_packets(
        "word " * 20, chars_per_line=24, mtu=23
    )
    check("fragments produced at MTU 23", len(fragmented) > 1, True)
    check("every fragment reports page 0 of 1",
          all((p[7], p[8]) == (0, 1) for p in fragmented), True)
    check("fragment index still carried for reassembly",
          [p[3] for p in fragmented], list(range(len(fragmented))))
    check("fragment count still carried",
          all(p[2] == len(fragmented) for p in fragmented), True)

    # Regression: build_text_packets used to split only BETWEEN wrapped lines.
    # At MTU 23 the capacity is 11 bytes, so a 24-character line produced a
    # 33-byte packet that G1Arm.write rejects with a ValueError. The header
    # checks above passed anyway, because nothing asserted the LENGTH.
    text = "Terminal connection established. System online."
    for mtu in (251, 185, 100, 23):
        pkts = controller.build_text_packets(text, mtu=mtu)
        cap = mtu - 3
        check(f"every packet fits ATT at MTU {mtu}",
              all(len(x) <= cap for x in pkts), True)
        check(f"payload reassembles at MTU {mtu}",
              b"".join(x[9:] for x in pkts).decode("utf-8", "replace")
              .startswith("Terminal"), True)

    print("\nasync pipeline checks")

    async def async_checks() -> None:
        config = build_llm_config("custom")
        config.api_key = "test"
        config.primary_model = "test-primary"
        config.compressor_model = "test-compressor"

        fake = _FakeClient([
            "A long detailed answer about Rayleigh scattering, wavelength "
            "dependence and atmospheric optical depth, with enough words to "
            "clearly exceed the display ceiling. " * 2,
            "Sky is blue because shorter wavelengths scatter more than longer ones.",
        ])
        s1 = await generate_primary_response("why", client=fake, config=config)
        check("stage 1 unconstrained", word_count(s1) > 25, True)
        s2 = await condense_for_glasses(s1, client=fake, config=config)
        check("stage 2 within char budget",
              len(s2) <= DEFAULT_MAX_DISPLAY_CHARS, True)
        check("stage 2 gets stage 1 text",
              s1[:40] in fake.completions.calls[1]["messages"][1]["content"], True)

        boom = _FakeClient(["x"], fail_at=0)
        try:
            await generate_primary_response("q", client=boom, config=config)
            check("stage 1 failure raises", False, True)
        except LLMStageError:
            check("stage 1 failure raises", True, True)

        written: list[str] = []

        async def send(text: str) -> None:
            written.append(text)

        partial = _FakeClient(
            ["Full answer text that is quite long and detailed here. " * 2], fail_at=1
        )
        got1 = await generate_primary_response("q", client=partial, config=config)
        try:
            await condense_for_glasses(got1, client=partial, config=config)
            check("stage 2 failure raises", False, True)
        except LLMStageError:
            check("stage 2 failure raises", True, True)

        from pipeline import run_query

        result = await run_query("q", send, client=partial, config=config, verbose=False)
        check("BLE still written after stage failure", len(written), 1)
        check("fallback within char budget",
              len(written[0]) <= DEFAULT_MAX_DISPLAY_CHARS, True)
        check("fallback non-empty", bool(written[0].strip()), True)

    asyncio.run(async_checks())

    print()
    total = ble_status + (1 if failures else 0)
    if total:
        print(f"selftest FAILED: {ble_status} BLE check(s), {len(failures)} pipeline check(s)")
        return 1
    print("selftest passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)

    if args.selftest:
        return selftest()

    if args.response == "write":
        args.response_mode = True
    elif args.response == "no-response":
        args.response_mode = False
    else:
        args.response_mode = None

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())