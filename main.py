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
import threading
import contextlib
import logging
import sys
import time
from datetime import datetime, timezone

from pathlib import Path

from bleak import BleakBackend
from bleak.backends import get_default_backend
from bleak.exc import BleakError

from g1_driver import EvenG1Controller, hexdump
from memory import FeedbackStore
from pipeline import (
    _lens_failure_line,
    check_glanceable,
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


def _indent(text: str, prefix: str = "  ") -> str:
    """Indent every line of a block, for embedding reports in CLI output."""
    return "\n".join(prefix + line for line in text.splitlines())


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

    ``input()`` runs off the loop so the loop keeps servicing the heartbeat
    and incoming TouchBar notifications while the user thinks. Calling it
    directly would freeze the session, and a frozen loop stops the heartbeat
    -- the G1 drops the link after ~32s idle.

    Deliberately NOT ``asyncio.to_thread``. That uses the loop's default
    executor, which ``asyncio.run`` joins during shutdown -- and the worker
    sitting in C-level ``input()`` cannot be interrupted, so Ctrl-C at the
    prompt appeared to do nothing and the process had to be killed or coaxed
    with an extra Enter. Verified: SIGINT mid-prompt, no newline, 124 (hung).

    A daemon thread fixes it: the interpreter never waits for one at exit, and
    nothing joins it.
    """
    loop = asyncio.get_running_loop()
    box: dict[str, object] = {}
    done = asyncio.Event()

    def _read() -> None:
        try:
            box["value"] = input(message)
        except BaseException as exc:        # noqa: BLE001 -- relayed below
            box["error"] = exc
        loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_read, daemon=True).start()
    await done.wait()
    if "error" in box:
        raise box["error"]                  # type: ignore[misc]
    return str(box.get("value", ""))


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
                learned = await session.learned()
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
                # :text is a deliberate raw path, so the text is NOT silently
                # budgeted -- an operator using it to reproduce an overflow
                # needs the overflow. But it used to overflow with no word at
                # all, which meant discovering the limit for the first time on
                # hardware. Warn on the terminal, where it costs nothing.
                over = fits_display(
                    rest, chars_per_line=args.chars_per_line) > 5
                try:
                    await build_sender(controller, args)(rest)
                    print(f"  sent {len(rest)} chars, "
                          f"{fits_display(rest, chars_per_line=args.chars_per_line)} "
                          f"lines")
                    if over:
                        print(f"  ! over the {DEFAULT_MAX_DISPLAY_CHARS}-char "
                              f"display budget -- the tail will not be visible "
                              f"on the lenses")
                except BleakError as exc:
                    print(f"  BLE error: {exc}")
                continue

            print(f"  unknown command ':{command}' — try :help")
            continue

        # -- a real question: the full pipeline ------------------------------
        try:
            card = await session.ask(line)
            print(f"\n>>> waveguide: {card.card.summary}")
            print(f"    ({len(card.card.summary)} chars, "
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
            path = save_overrides(
                overrides,
                ttl_s=(args.override_ttl_hours * 3600.0
                       if args.override_ttl_hours else None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        ttl = args.override_ttl_hours
        print(f"saved overrides -> {path}")
        if ttl:
            print(f"  they EXPIRE {ttl:g}h from now; the expiry is checked "
                  f"when the policy loads, so nothing has to remember them.")
        else:
            print("  no TTL set -- these persist until changed by hand. Pass "
                  "--override-ttl-hours if this is a test pin.")

    if args.weights:
        from memory import MIN_CARDS_FOR_INTEREST, FeedbackStore

        store = FeedbackStore(args.store)
        ctx = store.wearer_context()
        counts = store.counts()
        print(f"Learned topic weights  ({store.path})\n")
        print(f"  {counts.get('cards', 0)} cards, {counts.get('events', 0)} "
              f"events; a topic needs {MIN_CARDS_FOR_INTEREST} distinct cards "
              f"before it counts as an interest")
        if not counts.get("events"):
            print("\n  no reactions yet -- nothing to observe")

        ev = store.topic_evidence()
        shown = sorted(ev.items(), key=lambda kv: (-kv[1]["cards"],
                                                   -kv[1]["weight"], kv[0]))
        print()
        for term, info in shown[:25]:
            gate = "" if info["cards"] >= MIN_CARDS_FOR_INTEREST else "  (below gate)"
            print(f"  {term:<18} w={info['weight']:5.2f}  "
                  f"cards={info['cards']}  events={info['events']}{gate}")

        # Observation only. These weights used to be injected into a prompt,
        # which was measured and removed (documents/18); nothing downstream
        # reads them to write text, and saying so here stops the next reader
        # from assuming the prompt is one function call away.
        print("\n  no model sees any of this — it is shown to you, not to the "
              "LLM")
        if ctx.interests:
            print(f"  ranked interests: {', '.join(ctx.interests)}")
        if ctx.recent_summaries:
            print(f"  recent cards on the lenses: {len(ctx.recent_summaries)}")
        store.close()
        return 0

    if args.evidence:
        import interrupt as interrupt_mod
        from memory import FeedbackStore as EvidenceStore
        from policy import load as load_policy

        store = EvidenceStore(args.store)
        budget, prov = interrupt_mod.evidence_from_store(store)
        pol = load_policy()

        print("Policy evidence — what the ledger can and cannot say\n")
        print(f"  ledger      {store.path}")
        c = store.counts()
        print(f"  {c.get('cards', 0)} cards, {c.get('events', 0)} events")
        print(f"  drill_miss      {c.get('drill_miss', 0)}")
        print(f"  drill_deferred  {c.get('drill_deferred', 0)}"
              f"   (excluded from the rate by design)")
        print(f"  expand          {c.get('expand', 0)}")

        values = budget.evidence()
        # The same nested shape resolve() consumes, so this table cannot drift
        # from what the policy actually sees. Iterated over PROVENANCE, not
        # over the evidence dict, precisely so a metric with no source still
        # appears as "—" instead of silently disappearing from the report.
        print(f"\n  {'metric':<20}{'source':<14}value")
        print(f"  {'-' * 52}")
        for axis_id, metrics in values.items():
            for metric, value in metrics.items():
                print(f"  {metric:<20}{prov.get(metric, 'derived'):<14}{value}")
        for metric in ("label_days", "clean_pushes", "n_labels",
                       "queue_worth_rate", "rho"):
            if not any(metric in m for m in values.values()):
                print(f"  {metric:<20}{prov.get(metric, 'no source'):<14}"
                      f"—  <-- ABSENT, not zero")

        print(f"\n  policy as resolved:\n{_indent(pol.report(), '    ')}")
        manual = pol.manual()
        if manual:
            print(f"\n  MANUAL pins, NOT measurements: {', '.join(manual)}")
            print("  An axis here was set by hand, not by its deciding metric.")
            print("  settled_by_evidence() is false for these, so anything")
            print("  irreversible should refuse them without an explicit")
            print("  acknowledgement. `--policy-axes` shows the metrics.")
        store.close()
        return 0

    if args.queue_dir or args.queue:
        import interrupt as queue_mod
        from policy import load as load_policy

        pol = load_policy()
        cands = queue_mod.load_outbox(
            Path(args.queue_dir) if args.queue_dir else queue_mod.OUTBOX)
        budget = queue_mod.Budget()
        if args.queue_label_days:
            # Lets an operator see what the decision WOULD do with history
            # without having history. Printed loudly, because a dry run that
            # suddenly pushes is exactly the kind of thing that gets mistaken
            # for the real behaviour.
            budget.label_days = args.queue_label_days
        verdicts = queue_mod.plan(cands, budget=budget,
                                 overrides=pol.overrides)

        print(f"Interrupt queue — DRY RUN, nothing is sent to the glasses\n")
        print(f"  outbox     {args.queue_dir or queue_mod.OUTBOX}")
        print(f"  candidates {len(cands)}")
        if args.queue_label_days:
            print(f"  !! label_days forced to {args.queue_label_days}; this is "
                  f"NOT the default posture")
        print(f"\n  policy:\n{_indent(pol.report(), '    ')}")
        # A dry run that shows PUSH rows because somebody pinned an axis by
        # hand looks exactly like one showing them on evidence. Say which.
        if pol.manual():
            pinned = ", ".join(pol.manual())
            print(f"\n  MANUAL pins in effect: {pinned}")
            if pol.get("push_enabled") == "on" and not pol.settled_by_evidence(
                    "push_enabled"):
                print("  push_enabled=on is a HAND PIN, not a measurement. "
                      "Any live path must refuse this without an explicit "
                      "acknowledgement.")
        if not verdicts:
            print("\n  nothing waiting.")
        else:
            now = datetime.now(timezone.utc)
            print(f"\n  {'action':<6} {'gate':<12} {'G2':<4} {'src':<8} why")
            print(f"  {'-' * 78}")
            would_qualify = 0
            for v in verdicts:
                mark = "PUSH" if v.is_push else "queue"
                # Shadow column: what G2 alone would say, independent of the
                # policy axis. Without it a dry run where everything queues on
                # the axis tells an operator nothing about whether their
                # candidates would even qualify — and "policy blocked it" and
                # "it was never eligible" are completely different problems.
                g2_ok, _g2_why = v.candidate.eligible(now)
                would_qualify += bool(g2_ok)
                print(f"  {mark:<6} {v.gate:<12} {'yes' if g2_ok else 'no':<4} "
                      f"{v.candidate.source[:8]:<8} {v.reason}")
            print(f"\n  G2 column is shadow information, not the decision: "
                  f"{would_qualify}/{len(verdicts)} would qualify on the "
                  f"deterministic rules alone.")
            for v in verdicts:
                if v.gate == "eligibility":
                    print(f"    ineligible: {v.candidate.source} — {v.reason}")
        print(f"\n  {queue_mod.summarize(verdicts)}")
        return 0

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
    if args.two_stage:
        config.two_stage = True
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
        # Close the store too. It was previously never closed at all, which
        # leaked the per-thread connections; now that close() is safe against
        # live readers (it used to segfault), it is also correct to call.
        if session is not None:
            with contextlib.suppress(Exception):
                session.store.close()

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
        "--two-stage", action="store_true",
        help="use the original two-call pipeline (answer, then compress). OFF "
             "by default: measured 9-13x slower at identical answer relevance "
             "and no measured protection against invented numbers. Turn this "
             "on to compare, or to fall back if one-stage quality regresses.",
    )
    parser.add_argument(
        "--evidence", action="store_true",
        help="show which policy metrics can be derived from the ledger, "
             "which have no source yet, and how each axis resolves. This is "
             "the honest answer to 'why is the policy not changing?'.",
    )
    parser.add_argument(
        "--queue", action="store_true",
        help="run the interrupt decision over the outbox and print what "
             "would happen, then exit. DRY RUN: no radio, no glasses, no "
             "BLE write. Everything queues unless policy.push_enabled is on, "
             "which needs 14 days of label history.",
    )
    parser.add_argument(
        "--queue-dir", default=None, metavar="DIR",
        help="read candidates from DIR instead of ~/.g1/outbox. Pairs with "
             "--queue; useful for replaying a captured spool.",
    )
    parser.add_argument(
        "--queue-label-days", type=int, default=0, metavar="N",
        help="pretend N days of label history exist, to see what the "
             "decision would do with evidence. Off by default and printed "
             "loudly, because a dry run that pushes looks like the real "
             "posture.",
    )
    parser.add_argument(
        "--weights", action="store_true",
        help="show learned topic weights and the evidence behind them, and "
             "exit. Observation only: these weights are no longer shown to "
             "the model. Injection was removed after measurement "
             "(documents/18), but the substrate that produced them still "
             "measures cleanly and this is how you read it.",
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
             "(persists to the policy file). A pin is MANUAL, not evidence: "
             "it is reported as unresolved and `settled_by_evidence()` is "
             "false for it, so anything irreversible can refuse one. Pair "
             "with --override-ttl-hours so a test pin cannot outlive the test.",
    )
    parser.add_argument(
        "--override-ttl-hours", type=float, default=0.0, metavar="H",
        help="expire the overrides set by this command after H hours "
             "(0 = never expire). Expiry is checked when the policy is "
             "LOADED, so a pin lapses on its own rather than needing someone "
             "to remember it exists.",
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

    # Regression: the glanceability gate was structurally unsatisfiable. Its
    # score was digit + proper + time - lead/6 against a 0.5 floor, and `lead`
    # pinned to its 6.0 cap whenever no digit or lexicon word appeared early.
    # A line with a proper noun and nothing else therefore scored 1 - 1 = 0.0
    # and always failed, so the gate fired on ~67% of real model output and
    # sent good text into the repair path. Found by play-testing.
    print("\nglanceability gate")
    for good in (
        "Sky is blue because shorter wavelengths scatter more than longer ones",
        "Prince Hamlet seeks revenge on Claudius for murdering his father",
        "Boiling point of water at altitude: 95C",
        "Cheetahs have a small brain (120g), relying on speed not force",
    ):
        check(f"accepts {good[:28]!r}", check_glanceable(good).ok, True)
    for bad in (
        "ok",
        "This was fixed.",
        "It is important to note that results may vary.",
        "In conclusion, the situation has improved considerably overall",
    ):
        check(f"rejects {bad[:28]!r}", check_glanceable(bad).ok, False)

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

        # Regression: a real 401 used to be rendered onto the lenses as
        #   "LLM error. stage 1 (m) f" / "ailed: Error code: 401 -" /
        #   " {'error': {'message':"
        # -- three of five lines of exception repr, broken mid-word, with the
        # only useful part buried. Found by running the real pipeline.
        boom2 = _FakeClient(["x"], fail_at=0)
        err_written: list[str] = []

        async def send_err(text: str) -> None:
            err_written.append(text)

        await run_query("q", send_err, client=boom2, config=config, verbose=False)
        line = err_written[0] if err_written else ""
        check("lens failure line is short", len(line) <= 48, True)
        check("lens failure line has no repr punctuation",
              not any(ch in line for ch in "{}\'\""), True)
        check("lens failure line fits in 2 display lines",
              fits_display(line) <= 2, True)

    asyncio.run(async_checks())

    for err, expect in (
        ("Error code: 401 - {'error': {'message': 'bad key'}}", "auth"),
        ("404 model not found", "not found"),
        ("Request timed out after 30s", "timed out"),
        ("Connection refused", "unreachable"),
        (None, "No answer from model."),
    ):
        got = _lens_failure_line(err)
        check(f"lens line classifies {str(err)[:24]!r}", expect in got, True)

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