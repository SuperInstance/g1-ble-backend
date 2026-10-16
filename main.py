#!/usr/bin/env python3
"""
main.py
=======

CLI entry point for the Even Realities G1 developer backend.

Runs a two-stage AI pipeline and mirrors the result onto the waveguide:

    terminal query
        |
        v
    [ STAGE 1 ]  generate_primary_response()   heavy "thinker" model
        |          full, long-form answer, no length constraint
        v
    [ STAGE 2 ]  condense_for_glasses()        waveguide micro-compressor
        |          strict structural filter -> <= 25 words, no filler
        v
    [ BLE WRITE ] controller.send_text_packet() 0x4E packet -> both arms
                   (inside a try/except so a failed LLM call can never
                    tear down the active Bluetooth session)

Why two stages instead of one prompt: the heavy model is optimised for
reasoning quality and produces long answers that are unreadable in a
~25-word HUD window. Asking that same model to also compress tends to
degrade the reasoning. Splitting the roles keeps the thinking deep and
makes the display formatting a separate, cheap, easily-tunable step.

Examples::

    python main.py --selftest                # offline checks, no radio, no API key
    python main.py --llm-check               # verify LLM config without BLE
    python main.py                            # scan, connect, interactive REPL
    python main.py --one-shot                 # literal text, no LLM
    python main.py --ask "why is the sky blue"  # single pipeline run, then exit
    python main.py --scan-only                # list what is advertising
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass

from bleak import BleakBackend
from bleak.backends import get_default_backend
from bleak.exc import BleakError

from g1_driver import EvenG1Controller, hexdump, selftest

log = logging.getLogger("g1")

DEFAULT_TEST_TEXT = "Terminal connection established. System online."
DEFAULT_HOLD_SECONDS = 30.0

# Physical limit of the G1 waveguide. The official app fits ~24 characters
# per line and ~5 lines per screen; 25 words is a conservative upper bound
# that keeps a single response inside one screen.
DEFAULT_MAX_DISPLAY_WORDS = 25


# ---------------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    """
    Runtime configuration for both pipeline stages.

    Populated entirely from environment variables so the same code works
    against OpenAI and against any OpenAI-compatible local server
    (Ollama, LM Studio, vLLM, llama.cpp) without code changes.
    """

    api_key: str | None
    base_url: str | None
    primary_model: str
    compressor_model: str
    timeout: float
    max_retries: int
    max_display_words: int

    @property
    def target(self) -> str:
        """Human-readable description of where requests are going."""
        return self.base_url or "https://api.openai.com/v1"


def build_llm_config() -> LLMConfig:
    """
    Read LLM settings from the environment.

    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` are honoured automatically by the
    SDK as well; we read them explicitly so we can validate and report before
    any network call is attempted.
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("G1_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("G1_BASE_URL")

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print(f"  ! {name}={raw!r} is not a number; using {default}", file=sys.stderr)
            return default

    primary = (
        os.environ.get("G1_PRIMARY_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )
    compressor = os.environ.get("G1_COMPRESSOR_MODEL") or primary

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        primary_model=primary,
        compressor_model=compressor,
        timeout=_float("G1_LLM_TIMEOUT", 30.0),
        max_retries=int(os.environ.get("G1_LLM_RETRIES", "2") or 2),
        max_display_words=int(
            os.environ.get("G1_MAX_DISPLAY_WORDS", str(DEFAULT_MAX_DISPLAY_WORDS))
            or DEFAULT_MAX_DISPLAY_WORDS
        ),
    )


def describe_llm_config(config: LLMConfig) -> str:
    """One-line summary of the active LLM wiring, for the startup banner."""
    key_state = "set" if config.api_key else "MISSING"
    same = " (same as stage 1)" if config.compressor_model == config.primary_model else ""
    return (
        f"target={config.target}  stage1={config.primary_model}  "
        f"stage2={config.compressor_model}{same}  key={key_state}"
    )


def make_client(config: LLMConfig):
    """
    Build an ``AsyncOpenAI`` client.

    The import is deliberately lazy: the BLE half of this tool is fully
    functional without the ``openai`` package or an API key, and a user
    debugging TouchBar packets should not have to install anything extra.

    Chat Completions (``/v1/chat/completions``) is used rather than the
    OpenAI-only Responses API because every common local server implements it.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise LLMUnavailable(
            "the 'openai' package is not installed.\n"
            "    fix: pip install -r requirements.txt"
        ) from None

    if not config.api_key:
        raise LLMUnavailable(
            "no API key found.\n"
            "    set OPENAI_API_KEY for OpenAI, or point at a local server:\n"
            "      export OPENAI_BASE_URL=http://localhost:11434/v1\n"
            "      export OPENAI_API_KEY=ollama\n"
            "      export OPENAI_MODEL=qwen2.5:7b"
        )

    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )


class LLMUnavailable(RuntimeError):
    """Raised when the LLM half of the pipeline is not usable."""


class LLMStageError(RuntimeError):
    """Raised when a single stage fails. Never fatal to the BLE session."""


# ---------------------------------------------------------------------------
# STAGE 1 — the primary heavy thinker
# ---------------------------------------------------------------------------

PRIMARY_SYSTEM_PROMPT = (
    "You are a careful, knowledgeable assistant. Answer the user's question "
    "directly and completely.\n\n"
    "Write the answer you would normally write: reason it through, include the "
    "specifics that matter, and explain briefly where it is genuinely subtle. "
    "Do not compress or shorten it — a separate step handles that.\n\n"
    "Prefer plain prose. Use a short list only when the content is genuinely "
    "enumerable."
)


async def generate_primary_response(
    user_query: str,
    *,
    client=None,
    config: LLMConfig | None = None,
) -> str:
    """
    STAGE 1 — produce the full, unconstrained answer.

    This is the "heavy thinker": it does the actual reasoning and returns a
    normal long-form response. Length limiting is deliberately NOT applied
    here; that is Stage 2's job.

    ``client`` is injectable so the pipeline can be unit-tested without a
    network call (see :func:`selftest_pipeline`).

    Raises :class:`LLMStageError` on any failure. Callers must treat that as
    non-fatal for the BLE session.
    """
    config = config or build_llm_config()
    query = (user_query or "").strip()
    if not query:
        raise LLMStageError("empty query")

    client = client or make_client(config)

    try:
        text = await _chat(
            client,
            model=config.primary_model,
            system=PRIMARY_SYSTEM_PROMPT,
            user=query,
            max_tokens=1200,
            temperature=0.4,
        )
    except Exception as exc:
        raise LLMStageError(f"stage 1 ({config.primary_model}) failed: {exc}") from exc

    if not text.strip():
        raise LLMStageError("stage 1 returned an empty response")
    return text.strip()


# ---------------------------------------------------------------------------
# STAGE 2 — the waveguide micro-compressor
# ---------------------------------------------------------------------------

COMPRESSOR_SYSTEM_PROMPT = (
    "You are the display formatting engine for a head-up display built into a "
    "pair of smart glasses. You receive an assistant's full answer and emit ONLY "
    "the text that will be rendered on the lenses.\n\n"
    "HARD CONSTRAINTS — these are physical limits of the display, not style "
    "preferences:\n"
    "1. Output between 20 and 25 words. NEVER exceed 25 words. If the answer "
    "cannot be said in 25 words, drop everything except the core fact.\n"
    "2. No preamble, no greeting, no sign-off, no restating the question. No "
    "\"Sure\", no \"Here's\", no \"In summary\".\n"
    "3. No markdown: no bullet characters, no headers, no bold or italics, no "
    "code fences.\n"
    "4. No emoji, no quotation marks around the whole line.\n"
    "5. Lead with the single most important fact. Keep numbers, names, units "
    "and error codes — they survive truncation; drop connective prose first.\n"
    "6. Punchy and scannable, as if read in a second while walking.\n\n"
    "Output the display text and nothing else. No preamble, no explanation of "
    "your choices, no surrounding quotes."
)


async def condense_for_glasses(
    detailed_text: str,
    *,
    client=None,
    config: LLMConfig | None = None,
) -> str:
    """
    STAGE 2 — compress a full answer into what fits on the waveguide.

    Acts as a strict structural filter rather than a summariser: the model is
    told the display's hard limits and expected to obey them. Its output is
    then passed through :func:`enforce_word_budget`, because a word count that
    maps to a physical constraint is not something to take on faith.

    Raises :class:`LLMStageError` on failure.
    """
    config = config or build_llm_config()
    source = (detailed_text or "").strip()
    if not source:
        raise LLMStageError("nothing to condense")

    client = client or make_client(config)

    try:
        text = await _chat(
            client,
            model=config.compressor_model,
            system=COMPRESSOR_SYSTEM_PROMPT,
            user=(
                "Compress this answer into the glasses display format.\n\n"
                f"<answer>\n{source}\n</answer>"
            ),
            # Cap generation so the model cannot even produce a long answer.
            max_tokens=120,
            temperature=0.2,
        )
    except Exception as exc:
        raise LLMStageError(
            f"stage 2 ({config.compressor_model}) failed: {exc}"
        ) from exc

    if not text.strip():
        raise LLMStageError("stage 2 returned an empty response")

    return enforce_word_budget(text, config.max_display_words)


# ---------------------------------------------------------------------------
# Display post-processing (deterministic; never trust the model on the limit)
# ---------------------------------------------------------------------------

# Zero-width / decorative symbols the lens cannot render.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿️]+"
)
_MD_RE = re.compile(r"[*_`#>|~]+")
_BULLET_RE = re.compile(r"^\s*[-–—•*+]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}[.)]\s+", re.MULTILINE)
_PREAMBLE_RE = re.compile(
    r"^\s*(sure|certainly|of course|absolutely|okay|ok|here(?:'s| is)|"
    r"in summary|summary|to summarize|compressed:?|display text:?)\b[,:!\-—\s]*",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def strip_display_noise(text: str) -> str:
    """
    Remove characters and scaffolding the waveguide cannot render.

    Deliberately conservative: it only strips things that are guaranteed not
    to carry meaning (markdown, bullets, emoji, decorative quotes) plus a
    short list of filler prefixes. It never rewrites words.
    """
    if not text:
        return ""

    cleaned = text.replace("“", '"').replace("”", '"')
    cleaned = cleaned.replace("‘", "'").replace("’", "'")
    cleaned = _MD_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _NUMBERED_RE.sub("", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    # Models stack filler ("Sure, here's ..."), so strip repeatedly. Bounded
    # so a legitimate word that merely starts with "ok" cannot loop forever.
    for _ in range(3):
        stripped = _PREAMBLE_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    # Strip wrapping quotes the model may add around the whole line.
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    # Newlines collapse to spaces: the driver re-wraps for the display, and a
    # single-line fragment is what the lenses actually show.
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def enforce_word_budget(text: str, max_words: int) -> str:
    """
    Guarantee the final payload fits the display.

    The compressor is instructed to stay within budget, but a model that
    overruns must not be able to overflow the lenses, so the limit is applied
    here unconditionally as a hard backstop.
    """
    cleaned = strip_display_noise(text)
    if not cleaned:
        return ""

    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned

    truncated = " ".join(words[:max_words]).rstrip(",;:-—")
    return truncated + "…"


# ---------------------------------------------------------------------------
# LLM transport helper
# ---------------------------------------------------------------------------

async def _chat(
    client,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """
    One Chat Completions round-trip.

    Uses ``max_completion_tokens`` first and falls back to the older
    ``max_tokens`` parameter, because a number of local OpenAI-compatible
    servers only accept the legacy name.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )
    except Exception as exc:
        # Legacy-parameter fallback for servers without max_completion_tokens.
        if "max_completion_tokens" not in str(exc):
            raise
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return getattr(choices[0].message, "content", "") or ""


# ---------------------------------------------------------------------------
# The handoff: query -> stage 1 -> stage 2 -> BLE write
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Outcome of one full run through both stages."""

    query: str
    stage1: str | None
    stage2: str | None
    display_text: str
    stage1_error: str | None = None
    stage2_error: str | None = None


async def run_pipeline(
    query: str,
    controller: EvenG1Controller,
    *,
    client=None,
    config: LLMConfig | None = None,
    chars_per_line: int = 24,
    clear_first: bool = False,
    require_ack: bool = False,
    response_mode: bool | None = None,
) -> PipelineResult:
    """
    THE HANDOFF.

        query ──> [Stage 1] ──> [Stage 2] ──> [BLE 0x4E write]
                  heavy answer   <=25 words    both arms

    Every failure mode here is contained. An LLM error degrades to a fallback
    string that is still rendered on the glasses, and a BLE error propagates
    to the caller (which owns the session). The controller's heartbeat task is
    an independent asyncio task, so a slow LLM call of any length never
    starves the link — the G1's ~32s idle timeout is satisfied throughout.
    """
    config = config or build_llm_config()
    client = client or make_client(config)

    result = PipelineResult(query=query, stage1=None, stage2=None, display_text="")

    # --- Stage 1: heavy reasoning ------------------------------------------
    print("[1/2] thinking ...", flush=True)
    try:
        result.stage1 = await generate_primary_response(
            query, client=client, config=config
        )
        print(f"      {word_count(result.stage1)} words", flush=True)
    except LLMStageError as exc:
        result.stage1_error = str(exc)
        print(f"      stage 1 FAILED: {exc}", flush=True)

    # --- Stage 2: waveguide compression ------------------------------------
    if result.stage1:
        print("[2/2] condensing for waveguide ...", flush=True)
        try:
            result.stage2 = await condense_for_glasses(
                result.stage1, client=client, config=config
            )
            print(f"      {word_count(result.stage2)} words", flush=True)
        except LLMStageError as exc:
            result.stage2_error = str(exc)
            print(f"      stage 2 FAILED: {exc}", flush=True)

    # --- Fallback selection ------------------------------------------------
    # Prefer the compressor's output; fall back to a deterministic local
    # truncation of stage 1; finally surface a static error line. The glasses
    # always get *something*, and the BLE session is never torn down because
    # an API call failed.
    if result.stage2:
        result.display_text = result.stage2
    elif result.stage1:
        result.display_text = enforce_word_budget(result.stage1, config.max_display_words)
        print("      (using locally truncated stage 1 — compressor unavailable)")
    else:
        result.display_text = f"LLM error. {enforce_word_budget(_first_clause(result.stage1_error or 'no response'), 20)}"
        print("      (no LLM output available)")

    if not result.display_text.strip():
        result.display_text = "No response."

    # --- BLE write: the final handoff --------------------------------------
    # This is the only part that touches the radio. It runs last so that a
    # slow or failing LLM never delays or interrupts the link.
    print(f"[ble] writing {word_count(result.display_text)} words to both arms ...",
          flush=True)
    packets = await controller.send_text_packet(
        result.display_text,
        chars_per_line=chars_per_line,
        clear_first=clear_first,
        require_ack=require_ack,
        response=response_mode,
    )
    print(f"[ble] {len(packets)} packet(s) written: "
          f"{hexdump(packets[0])[:48]}...", flush=True)

    return result


def word_count(text: str | None) -> int:
    """Whitespace-delimited word count; 0 for empty/None."""
    return len((text or "").split())


def _first_clause(text: str, limit: int = 60) -> str:
    """First sentence/clause of an error string, for the fallback display."""
    match = re.split(r"[.\n]", text.strip(), maxsplit=1)
    first = (match[0] if match else text).strip()
    return first[:limit]


# ---------------------------------------------------------------------------
# Bluetooth helpers (unchanged from the BLE-only phase)
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
        remaining = deadline - now
        if remaining <= 0:
            break

        if now - last_report >= 10.0:
            last_report = now
            print(
                f"  ... {humanise_duration(now - start)} elapsed, "
                f"{humanise_duration(remaining)} left, "
                f"{len(controller._packet_log)} packet(s) received"
            )

        await asyncio.sleep(0.1)

    elapsed = time.monotonic() - start
    print(
        f"\nMonitor window closed after {humanise_duration(elapsed)}: "
        f"{len(controller._packet_log)} packet(s) received."
    )
    return len(controller._packet_log)


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

REPL_HELP = """
Commands:
  <any text>        run the two-stage pipeline and mirror it to the glasses
  :text <literal>   send literal text to the glasses, bypassing the LLM
  :stage1 <query>   run only stage 1 and print the full answer (no BLE write)
  :battery          query both arms for battery level
  :stats            packet tally for this session
  :config           show the active LLM configuration
  :help             this message
  :quit             disconnect and exit

TouchBar packets are printed as they arrive, interleaved with the prompt.
""".strip()


async def prompt_user(message: str) -> str:
    """
    Read one line without blocking the event loop.

    ``input()`` is moved onto a worker thread so the loop keeps running: the
    heartbeat task keeps the G1 link alive and incoming TouchBar
    notifications keep printing while the user is thinking. Calling ``input()``
    directly would freeze the whole session for as long as the user waits.
    """
    return await asyncio.to_thread(input, message)


async def repl(
    controller: EvenG1Controller,
    args: argparse.Namespace,
    *,
    config: LLMConfig | None = None,
) -> int:
    """
    Interactive query loop.

    The BLE connection stays open for the whole session; queries are handled
    one at a time and each failure is contained so the session survives.
    """
    config = config or build_llm_config()
    turns = 0

    print("\n" + "=" * 66)
    print("G1 interactive session — type a question, or :help")
    print(f"LLM: {describe_llm_config(config)}")
    print("=" * 66)

    while True:
        try:
            line = await prompt_user("\ng1> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        line = (line or "").strip()
        if not line:
            continue

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
                print(describe_llm_config(config))
                continue
            if command == "stats":
                summarise_packets(controller)
                continue
            if command == "battery":
                try:
                    print(f"\nBattery: {await controller.query_battery()}")
                except BleakError as exc:
                    print(f"\nBLE error: {exc}")
                continue
            if command == "stage1":
                if not rest:
                    print("  usage: :stage1 <query>")
                    continue
                print("\n[1/2] thinking ...", flush=True)
                try:
                    answer = await generate_primary_response(
                        rest, config=config
                    )
                    print(f"\n--- stage 1 ({word_count(answer)} words) ---\n{answer}\n")
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
                    await controller.send_text_packet(
                        rest,
                        chars_per_line=args.chars_per_line,
                        clear_first=args.clear,
                        response=args.response_mode,
                    )
                    print(f"  sent {word_count(rest)} words")
                except BleakError as exc:
                    print(f"  BLE error: {exc}")
                continue

            print(f"  unknown command ':{command}' — try :help")
            continue

        # --- normal query: the full pipeline -------------------------------
        turns += 1
        started = time.monotonic()
        try:
            result = await run_pipeline(
                line,
                controller,
                config=config,
                chars_per_line=args.chars_per_line,
                clear_first=args.clear,
                require_ack=args.require_ack,
                response_mode=args.response_mode,
            )
            print(f"\n>>> waveguide: {result.display_text}")
            print(
                f"    ({humanise_duration(time.monotonic() - started)}, "
                f"{word_count(result.display_text)} words on display)"
            )
        except LLMUnavailable as exc:
            # Cannot even build a client. Not fatal — keep the BLE session up
            # so the user can still use :text and watch TouchBar packets.
            print(f"\nLLM unavailable: {exc}")
            print("    The glasses stay connected. Use :text to send literal text.")
        except LLMStageError as exc:
            print(f"\nstage error: {exc}")
        except BleakError as exc:
            print(f"\nBLE error: {exc}")
            print("    The session is still up — try :quit and reconnect.")

    return turns


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    # -- llm-check short-circuits before touching the radio -----------------
    if args.llm_check:
        config = build_llm_config()
        if args.max_display_words:
            config.max_display_words = args.max_display_words
        print(f"LLM config: {describe_llm_config(config)}")
        try:
            client = make_client(config)
        except LLMUnavailable as exc:
            sys.stdout.flush()
            print(f"\nLLM not configured:\n{exc}", file=sys.stderr)
            return 2
        print("\nprobing stage 2 with a one-word test input ...")
        try:
            probe = await condense_for_glasses(
                "The capital of France is Paris.", client=client, config=config
            )
        except LLMStageError as exc:
            sys.stdout.flush()
            print(f"\nprobe failed: {exc}", file=sys.stderr)
            return 1
        print(f"probe ok -> {probe!r} ({word_count(probe)} words)")
        return 0

    preflight()

    config = build_llm_config()
    # An explicit flag overrides the environment value.
    if args.max_display_words:
        config.max_display_words = args.max_display_words
    if not args.no_text:
        print(f"LLM: {describe_llm_config(config)}")

    controller = EvenG1Controller(
        scan_timeout=args.scan_timeout,
        mtu=args.mtu,
        echo_packets=not args.quiet,
    )

    print(f"\nScanning for G1 arms for {args.scan_timeout:.0f}s ...")
    exit_code = 0

    try:
        if args.scan_only:
            result_scan = await controller.discover(
                timeout=args.scan_timeout,
                require_both_arms=not args.allow_single_arm,
            )
            print(f"\n{result_scan.describe()}  (in {result_scan.elapsed:.1f}s)")
            for identity in result_scan.identities.values():
                print(f"  {identity.label}")
            for identity in result_scan.nus_only:
                print(f"  [nus-only, not claimed as G1] {identity.label}")
            if not result_scan.channels:
                print(
                    "\nNothing matched. Confirm the glasses are powered on and "
                    "not bonded to the Even Realities app."
                )
                return 1
            return 0

        print("Connecting ...")
        result = await controller.connect(
            channel=args.channel,
            allow_single_arm=args.allow_single_arm,
        )
        print(f"  {result.describe()}")

        for arm in controller.arms:
            print(
                f"  {arm.identity.side} arm {arm.identity.address} "
                f"connected, MTU={arm.mtu}"
            )

        if len(controller.arms) < 2:
            print(
                "\n  ! Connected to only one arm. Text will appear on that side "
                "only.\n    Re-run with the other arm awake and unpaired."
            )

        if args.dump_services:
            controller.dump_services()

        if args.battery:
            levels = await controller.query_battery()
            print(f"\nBattery: {levels}")

        # -- single query mode: --ask "..." ---------------------------------
        if args.ask:
            print(f"\nQuery: {args.ask}")
            try:
                outcome = await run_pipeline(
                    args.ask,
                    controller,
                    config=config,
                    chars_per_line=args.chars_per_line,
                    clear_first=args.clear,
                    require_ack=args.require_ack,
                    response_mode=args.response_mode,
                )
                print(f"\n>>> waveguide: {outcome.display_text}")
            except LLMUnavailable as exc:
                print(f"\nLLM unavailable: {exc}", file=sys.stderr)
                exit_code = 2
            return exit_code

        # -- literal text mode: --text / --no-text --------------------------
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

        # -- interactive session ---------------------------------------------
        if args.interactive or (sys.stdin.isatty() and not args.one_shot):
            await repl(controller, args, config=config)
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
        # Always tear the link down, even on failure, so BlueZ releases the
        # adapter and the next run can reconnect immediately.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.disconnect(), timeout=10.0)

    if exit_code == 0 and controller._packet_log and not args.interactive:
        summarise_packets(controller)

    print("done.")
    return exit_code


# ---------------------------------------------------------------------------
# Offline self-test (no radio, no API key, no network)
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
    """Minimal stand-in for ``client.chat.completions``."""

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


def _test_config() -> LLMConfig:
    return LLMConfig(
        api_key="test",
        base_url="http://localhost:11434/v1",
        primary_model="test-primary",
        compressor_model="test-compressor",
        timeout=5.0,
        max_retries=0,
        max_display_words=DEFAULT_MAX_DISPLAY_WORDS,
    )


async def _async_checks() -> list[str]:
    """Async portion of the self-test. Returns a list of failure labels."""
    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual == expected:
            print(f"  ok   {label}")
        else:
            print(
                f"  FAIL {label}\n"
                f"         expected {expected!r}\n"
                f"         actual   {actual!r}"
            )
            failures.append(label)

    config = _test_config()

    print("\ntwo-stage handoff")
    fake = _FakeClient([
        "A long, detailed answer about Rayleigh scattering and wavelength. " * 3,
        "Sky is blue because shorter wavelengths scatter more than longer ones.",
    ])
    stage1 = await generate_primary_response("why is the sky blue", client=fake, config=config)
    check("stage 1 is unconstrained", word_count(stage1) > 25, True)
    check("stage 1 used the primary model", fake.completions.calls[0]["model"], "test-primary")

    stage2 = await condense_for_glasses(stage1, client=fake, config=config)
    check("stage 2 used the compressor model", fake.completions.calls[1]["model"], "test-compressor")
    check("stage 2 within budget", word_count(stage2) <= 25, True)

    stage2_messages = fake.completions.calls[1]["messages"]
    check(
        "stage 2 receives the stage 1 text",
        stage1[:40] in stage2_messages[1]["content"],
        True,
    )
    check(
        "stage 2 system prompt is the HUD prompt",
        "head-up display" in stage2_messages[0]["content"],
        True,
    )

    print("\nstage failure containment")
    # A stage-1 failure means the very first call raises (index 0).
    boom = _FakeClient(["x"], fail_at=0)
    try:
        await generate_primary_response("q", client=boom, config=config)
        check("stage 1 failure raises LLMStageError", False, True)
    except LLMStageError:
        check("stage 1 failure raises LLMStageError", True, True)

    # Stage 2 dies -> stage 1 output must still be usable locally.
    half = _FakeClient(["Full answer text that is quite long and detailed here."], fail_at=1)
    got1 = await generate_primary_response("q", client=half, config=config)
    try:
        await condense_for_glasses(got1, client=half, config=config)
        check("stage 2 failure raises LLMStageError", False, True)
    except LLMStageError:
        check("stage 2 failure raises LLMStageError", True, True)
    fallback = enforce_word_budget(got1, config.max_display_words)
    check("local fallback is non-empty", bool(fallback.strip()), True)
    check("local fallback within budget", word_count(fallback) <= 25, True)

    return failures


def selftest_pipeline() -> int:
    """
    Verify both the BLE packet format and the LLM pipeline offline.

    Run with ``python main.py --selftest``. Uses a fake LLM client, so no API
    key and no network access are required.
    """
    print("BLE packet format")
    ble_status = selftest()

    print("\ndisplay post-processing")
    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual == expected:
            print(f"  ok   {label}")
        else:
            print(
                f"  FAIL {label}\n"
                f"         expected {expected!r}\n"
                f"         actual   {actual!r}"
            )
            failures.append(label)

    check("strips markdown", strip_display_noise("**bold** text"), "bold text")
    check("strips bullets", strip_display_noise("- first\n- second"), "first second")
    check("strips emoji", strip_display_noise("hello \U0001F600 world"), "hello world")
    check(
        "strips stacked preamble",
        strip_display_noise("Sure, here's the thing"),
        "the thing",
    )
    check("strips wrapping quotes", strip_display_noise('"quoted"'), "quoted")

    long_text = " ".join(f"word{i}" for i in range(60))
    trimmed = enforce_word_budget(long_text, 25)
    check("enforces word limit", word_count(trimmed) <= 25, True)
    check("marks truncation", trimmed.endswith("…"), True)
    check("leaves short text alone", enforce_word_budget("hi there", 25), "hi there")
    check("handles empty input", enforce_word_budget("", 25), "")

    print("\nasync pipeline checks")
    failures.extend(asyncio.run(_async_checks()))

    print()
    total = ble_status + (1 if failures else 0)
    if total:
        print(f"selftest FAILED: {ble_status} BLE check(s), {len(failures)} pipeline check(s)")
        return 1
    print("selftest passed")
    return 0


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
            "  export OPENAI_API_KEY=sk-...\n"
            "  python main.py                       # interactive session\n"
            "  python main.py --llm-check            # test the LLM path only\n"
            "  python main.py --ask 'why is the sky blue'\n"
            "  python main.py --one-shot --text 'hello'\n\n"
            "local model:\n"
            "  export OPENAI_BASE_URL=http://localhost:11434/v1\n"
            "  export OPENAI_API_KEY=ollama\n"
            "  export OPENAI_MODEL=qwen2.5:7b\n"
            "  export G1_COMPRESSOR_MODEL=qwen2.5:3b\n"
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
        "--llm-check", action="store_true",
        help="validate LLM configuration and probe stage 2, then exit",
    )
    parser.add_argument(
        "--max-display-words", type=int, default=DEFAULT_MAX_DISPLAY_WORDS,
        metavar="N",
        help=f"hard display word limit (default: {DEFAULT_MAX_DISPLAY_WORDS})",
    )

    # -- payload ----------------------------------------------------------
    parser.add_argument(
        "--text", metavar="STR", nargs="?", const=DEFAULT_TEST_TEXT,
        default=DEFAULT_TEST_TEXT,
        help=f"literal text to render, bypassing the LLM (default: {DEFAULT_TEST_TEXT!r})",
    )
    parser.add_argument(
        "--no-text", action="store_true",
        help="do not send the default literal text on startup",
    )

    # -- connection -------------------------------------------------------
    parser.add_argument(
        "--hold", type=float, default=DEFAULT_HOLD_SECONDS, metavar="SEC",
        help=f"seconds to monitor TouchBars in non-interactive mode (default: {DEFAULT_HOLD_SECONDS:.0f})",
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=12.0, metavar="SEC",
        help="seconds to scan for the glasses (default: 12)",
    )
    parser.add_argument(
        "--channel", metavar="XX",
        help="pin a specific G1 channel number (e.g. 07) instead of auto-selecting",
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
        help="run offline packet + pipeline checks and exit (no radio, no API key)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="suppress per-packet RX lines",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug logging, including BLE writes and MTU negotiation",
    )

    return parser


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
        return selftest_pipeline()

    # argparse's nargs="?" gives `const` for a bare `--text`, but no default,
    # so only substitute when the flag was supplied without a value.
    if args.text is not None and args.text == "":
        args.text = DEFAULT_TEST_TEXT

    # argparse yields a string here; the driver wants True/False/None.
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