"""
pipeline.py
===========

The LLM half of the system, with no knowledge of Bluetooth.

Two stages, deliberately separated:

* **Stage 1** reasons and writes a normal, complete answer.
* **Stage 2** is a structural filter that squeezes that answer into the ~25
  words the G1 waveguide can actually show.

Keeping this layer free of BLE concerns means it can be driven by the glasses,
by a test harness, or by a future front-end without change. The BLE side just
supplies an ``await send(text)`` callback.

Everything here is provider-agnostic: any server implementing
``POST {base_url}/chat/completions`` works, local or cloud.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

log = logging.getLogger("g1.pipeline")

DEFAULT_MAX_DISPLAY_WORDS = 25

# The waveguide is the real constraint, and it is measured in CHARACTERS, not
# words. The official demo app lays out 5 lines of ~24 characters for text, so
# one screen holds roughly 120 characters. A 25-word cap is not equivalent:
# at ~5.7 characters per word, 25 words runs to ~140 characters and reliably
# spills onto a sixth line. Words are kept as a secondary ceiling because they
# track readability, but characters are what actually decide whether the text
# fits.
DEFAULT_MAX_DISPLAY_CHARS = 120
DISPLAY_CHARS_PER_LINE = 24
DISPLAY_LINES = 5


class LLMUnavailable(RuntimeError):
    """The LLM half is not usable at all (no client, no key, no server)."""


class LLMStageError(RuntimeError):
    """A single stage failed. Never fatal to the surrounding session."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    """
    A known endpoint speaking the OpenAI Chat Completions protocol.

    These are presets, not a whitelist. Anything else works by setting a base
    URL, via ``OPENAI_BASE_URL`` or the ``custom`` provider.
    """

    name: str
    base_url: str | None
    key_env: tuple[str, ...]
    default_primary: str | None
    default_compressor: str | None
    local: bool = False  # local servers generally do not validate the key


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "openai", None, ("OPENAI_API_KEY",),
        "gpt-4o-mini", "gpt-4o-mini",
    ),
    "ollama": Provider(
        "ollama", "http://127.0.0.1:11434/v1", ("OLLAMA_API_KEY",),
        None, None, local=True,
    ),
    "lmstudio": Provider(
        "lmstudio", "http://127.0.0.1:1234/v1", ("LMSTUDIO_API_KEY",),
        None, None, local=True,
    ),
    "llamacpp": Provider(
        "llamacpp", "http://127.0.0.1:8080/v1", ("LLAMACPP_API_KEY",),
        None, None, local=True,
    ),
    "vllm": Provider(
        "vllm", "http://127.0.0.1:8000/v1", ("VLLM_API_KEY",),
        None, None, local=True,
    ),
    "openrouter": Provider(
        "openrouter", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",),
        "openai/gpt-4o-mini", "openai/gpt-4o-mini",
    ),
    "groq": Provider(
        "groq", "https://api.groq.com/openai/v1", ("GROQ_API_KEY",),
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    ),
    "together": Provider(
        "together", "https://api.together.xyz/v1", ("TOGETHER_API_KEY",),
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "deepseek": Provider(
        "deepseek", "https://api.deepseek.com/v1", ("DEEPSEEK_API_KEY",),
        "deepseek-chat", "deepseek-chat",
    ),
    "custom": Provider(
        "custom", None, ("OPENAI_API_KEY", "G1_API_KEY"),
        None, None, local=False,
    ),
}

LOCAL_PLACEHOLDER_KEY = "not-needed"


@dataclass
class LLMConfig:
    """Resolved runtime configuration for both stages."""

    api_key: str | None
    base_url: str | None
    primary_model: str | None
    compressor_model: str | None
    timeout: float
    max_retries: int
    max_display_words: int
    provider: str = "custom"
    max_display_chars: int = DEFAULT_MAX_DISPLAY_CHARS
    # One call, or two (reason then compress).
    #
    # This was the shipped design, justified in DESIGN.md with "asking one
    # model to do both degrades the reasoning". That was never measured. It is
    # now, against qwen2.5-coder:7b, and the claim did not survive:
    #
    #   latency    one 0.26s vs two 2.38s median   (9.2x, reproduced at 13.4x
    #              on an 18-question corpus)
    #   recall     19/20 vs 19/20 key facts answered
    #   invented   1 vs 3 numbers, of which one of each is a real error and
    #              the rest are checker false positives -- so n=20 cannot
    #              distinguish the arms
    #
    # The expensive option's stated justification is not supported and it
    # costs an order of magnitude. So the default is now ONE call. The burden
    # of proof sits with the expensive path: set G1_TWO_STAGE=1 or pass
    # --two-stage to put it back and compare.
    two_stage: bool = False

    @property
    def target(self) -> str:
        return self.base_url or "https://api.openai.com/v1"

    @property
    def is_local(self) -> bool:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"]).local


def build_llm_config(provider: str | None = None) -> LLMConfig:
    """
    Resolve settings from a provider preset plus environment overrides.

    Environment variables always win over preset defaults, so a preset is
    only ever a starting point.
    """
    name = (provider or os.environ.get("G1_PROVIDER") or "openai").strip().lower()
    preset = PROVIDERS.get(name)
    if preset is None:
        raise LLMUnavailable(
            f"unknown provider {name!r}. Known: {', '.join(sorted(PROVIDERS))}.\n"
            "    Or set OPENAI_BASE_URL for any OpenAI-compatible server."
        )

    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("G1_BASE_URL")
        or preset.base_url
    )

    api_key: str | None = None
    for var in (*preset.key_env, "OPENAI_API_KEY", "G1_API_KEY"):
        value = os.environ.get(var)
        if value:
            api_key = value
            break
    if api_key is None and preset.local:
        api_key = LOCAL_PLACEHOLDER_KEY

    primary = (
        os.environ.get("G1_PRIMARY_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or preset.default_primary
    )
    compressor = os.environ.get("G1_COMPRESSOR_MODEL") or preset.default_compressor
    # A preset's compressor default names a model on the PRESET's endpoint.
    # If the caller has redirected the base URL somewhere else -- a local
    # Ollama, a proxy -- that name is very likely wrong there, and the failure
    # is a 404 about a model the operator never asked for. Found by actually
    # running it: `OPENAI_MODEL=qwen2.5-coder:7b` gave stage 1 the local model
    # and left stage 2 asking OpenAI for gpt-4o-mini.
    if base_url != preset.base_url:
        compressor = os.environ.get("G1_COMPRESSOR_MODEL") or primary

    def _float(name_: str, default: float) -> float:
        raw = os.environ.get(name_)
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        primary_model=primary,
        compressor_model=compressor,
        timeout=_float("G1_LLM_TIMEOUT", 30.0),
        max_retries=int(os.environ.get("G1_LLM_RETRIES", "2") or 2),
        two_stage=(os.environ.get("G1_TWO_STAGE", "0") or "0") == "1",
        max_display_words=int(
            os.environ.get("G1_MAX_DISPLAY_WORDS", str(DEFAULT_MAX_DISPLAY_WORDS))
            or DEFAULT_MAX_DISPLAY_WORDS
        ),
        max_display_chars=int(
            os.environ.get("G1_MAX_DISPLAY_CHARS", str(DEFAULT_MAX_DISPLAY_CHARS))
            or DEFAULT_MAX_DISPLAY_CHARS
        ),
        provider=preset.name,
    )


def describe_llm_config(config: LLMConfig) -> str:
    key_state = "set" if config.api_key else "MISSING"
    primary = config.primary_model or "(auto)"
    compressor = config.compressor_model or "(auto)"
    same = " (same as stage 1)" if compressor == primary else ""
    return (
        f"provider={config.provider}  target={config.target}  "
        f"stage1={primary}  stage2={compressor}{same}  key={key_state}"
    )


def make_client(config: LLMConfig):
    """
    Build an ``AsyncOpenAI`` client.

    The import is lazy so the BLE half of the tool works with neither the
    ``openai`` package nor an API key installed.

    Chat Completions is used rather than the OpenAI-only Responses API,
    because every common local server implements it.
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
            f"no API key found for provider {config.provider!r}.\n"
            "    cloud:  export OPENAI_API_KEY=sk-...\n"
            f"    local:  python main.py --provider ollama   (no key needed)\n"
            f"    known providers: {', '.join(sorted(PROVIDERS))}"
        )

    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

async def list_available_models(config: LLMConfig, client=None) -> list[str]:
    """Ask the endpoint which models it serves, via standard ``GET /models``."""
    client = client or make_client(config)
    try:
        page = await client.models.list()
    except Exception as exc:
        raise LLMUnavailable(
            f"could not list models at {config.target}: {exc}\n"
            "    Set G1_PRIMARY_MODEL explicitly to skip discovery."
        ) from exc

    ids: list[str] = []
    for entry in getattr(page, "data", None) or []:
        ident = getattr(entry, "id", None) or (
            entry.get("id") if isinstance(entry, dict) else None
        )
        if ident:
            ids.append(ident)
    return ids


async def ensure_models(config: LLMConfig, client=None) -> LLMConfig:
    """
    Fill in any missing model name by discovering what the server has.

    Local presets ship with no default name on purpose, so a fresh checkout
    works against whatever the user has already pulled. The choice is printed
    rather than made silently.
    """
    if config.primary_model and config.compressor_model:
        return config

    available = await list_available_models(config, client)
    if not available:
        raise LLMUnavailable(
            f"{config.target} reported no models.\n"
            "    With Ollama: ollama pull qwen2.5:7b"
        )

    chosen = config.primary_model or available[0]
    print(
        f"[llm] {config.target} serves {len(available)} model(s); using {chosen!r}",
        flush=True,
    )
    if len(available) > 1:
        print(f"[llm] available: {', '.join(available)}", flush=True)

    config.primary_model = chosen
    if not config.compressor_model:
        config.compressor_model = chosen
    return config


# ---------------------------------------------------------------------------
# Prompts
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

FOLLOWUP_SYSTEM_PROMPT = (
    "You are continuing a conversation on a pair of smart glasses.\n\n"
    "The wearer read a summary and asked for MORE. That means it was useful "
    "but not sufficient. You are given the original question and everything "
    "already shown to them.\n\n"
    "Write the NEXT most useful increment of information:\n"
    "- Do not repeat anything already shown.\n"
    "- Do not restate or recap the question.\n"
    "- Lead with the new information, not with a preamble.\n"
    "- Go deeper: the mechanism, the number, the caveat, the concrete next "
    "step.\n"
    "- If the topic has a natural continuation worth surfacing, name it.\n\n"
    "Still write a complete passage. A later step decides what actually "
    "reaches the lenses, so do not compress it yourself."
)

COMPRESSOR_SYSTEM_PROMPT = (
    "You are the display formatting engine for a head-up display built into a "
    "pair of smart glasses. You receive an assistant's full answer and emit ONLY "
    "the text that will be rendered on the lenses.\n\n"
    "HARD CONSTRAINTS — these are physical limits of the display, not style "
    "preferences:\n"
    "1. NEVER exceed 25 words, and NEVER exceed 120 characters. This is a hard\n"
    "   physical cap; going over means the text does not fit on the lenses.\n"
    "2. Aim for 20-25 words. If your first draft is shorter, add the next most "
    "important concrete detail (a number, a name, a mechanism) rather than "
    "padding.\n"
    "3. No preamble, no greeting, no sign-off, no restating the question. No "
    "\"Sure\", no \"Here's\", no \"In summary\".\n"
    "4. No markdown: no bullet characters, no headers, no bold or italics, no "
    "code fences.\n"
    "5. No emoji, no quotation marks around the whole line.\n"
    "6. Lead with the single most important fact. Keep numbers, names, units "
    "and error codes — they survive truncation; drop connective prose first.\n"
    "7. Punchy and scannable, as if read in a second while walking.\n\n"
    "Output the display text and nothing else. No preamble, no explanation of "
    "your choices, no surrounding quotes."
)

# REMOVED: the wearer-interest block (both variants).
#
# There were two of them -- WEARER_CONTEXT_TEMPLATE for stage 2 and
# ONE_STAGE_INTEREST_BLOCK for the one-call path -- and between them they had
# grown eight rules about how to be harmless. That effort is the tell. Every
# clause was a patch for a failure the mechanism itself caused, because a
# block that "has no authority" is still a block the model reads.
#
# Measured, not assumed (experiments/personalisation_ab.py, documents/18):
#   - the gate fired 0/8 on ordinary questions, so there was nothing to gain;
#   - when it did fire (n=3) it shifted answer polarity and destabilised it;
#   - it cost 0.44s against 0.25s for the plain prompt.
# So the block is removed, not retuned: the failure was not the wording.
#
# The substrate is NOT removed. memory.py still tracks topic weights, and
# `main.py --weights` still reports them -- they measure cleanly and they are
# how a human observes the wearer. What was falsified is telling the MODEL
# about them. The exact prompt text lives on in the experiment that falsified
# it, so the measurement stays reproducible.
#
# What is left is smaller than it looks. After this removal the topic weights
# have exactly ONE consumer in the shipped package: `main.py --weights` and
# the log line in Session._record. They do not rank the interruption queue --
# that gate reads `queue_worth_rate` over review labels (see policy.py) -- and
# they do not touch any prompt. Per-card `keywords` are still stored, but no
# shipped code consumes them either. So the honest description of the learning
# substrate today is: measured, logged, and not yet load-bearing. Whatever
# eventually reads it should be measured on its own terms rather than assumed
# to inherit the credibility of a clean substrate.


# ---------------------------------------------------------------------------
# Display post-processing (deterministic)
# ---------------------------------------------------------------------------

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

    Conservative by design: it strips only what cannot carry meaning (markdown,
    bullets, emoji, decorative quotes) plus a short list of filler prefixes.
    It never rewrites words.
    """
    if not text:
        return ""

    cleaned = text.replace("“", '"').replace("”", '"')
    cleaned = cleaned.replace("‘", "'").replace("’", "'")
    cleaned = _MD_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _NUMBERED_RE.sub("", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    # Models stack filler ("Sure, here's ..."), so strip repeatedly. Bounded so
    # a legitimate word beginning with "ok" cannot loop forever.
    for _ in range(3):
        stripped = _PREAMBLE_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    # Newlines collapse to spaces: the driver re-wraps, and the lenses show one
    # short line at a time.
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def enforce_display_budget(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_DISPLAY_CHARS,
    max_words: int = DEFAULT_MAX_DISPLAY_WORDS,
) -> str:
    """
    Guarantee the payload physically fits the waveguide.

    Two ceilings, both applied unconditionally: a character ceiling that maps
    to the screen, and a word ceiling that tracks readability. Whichever binds
    first wins, and truncation always lands on a word boundary so a word is
    never cut in half.

    The compressor is told these limits, but a limit tied to physical
    dimensions is not taken on faith from a language model: if it overruns, it
    is cut here before anything reaches the BLE stack.
    """
    cleaned = strip_display_noise(text)
    if not cleaned:
        return ""

    words = cleaned.split()

    # Fit the character ceiling first, then the word ceiling.
    kept: list[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if len(candidate) > max_chars:
            break
        kept.append(word)

    # Short-circuit the common case: it already fits both ceilings.
    if len(kept) == len(words) and len(kept) <= max_words:
        return cleaned

    if not kept:
        # A single word longer than the screen: hard-cut it so we still send
        # something rather than silently dropping the whole payload.
        return cleaned[: max(1, max_chars - 1)] + "…"

    result = " ".join(kept[:max_words]).rstrip(",;:-—")
    if len(kept) < len(words) or len(kept) > max_words:
        result += "…"
    return result


def fits_display(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_DISPLAY_CHARS,
    chars_per_line: int = DISPLAY_CHARS_PER_LINE,
) -> int:
    """
    How many lines ``text`` occupies on the waveguide.

    Exposed so the driver and any UI can agree on one definition of "fits"
    instead of each inventing its own.
    """
    cleaned = strip_display_noise(text)
    if not cleaned:
        return 0
    per_line = max(1, chars_per_line)
    return (len(cleaned) + per_line - 1) // per_line


# Retained name only. This wrapper had nine call sites until the
# enforce_display_budget refactor (1b1f5c1) moved them all; it was kept with a
# comment claiming those call sites still existed, which they did not, for as
# long as the claim stood. `experiments/wiring_audit.py` reports it as uncalled,
# which is correct. Kept because removing a public name is a breaking change
# for anything importing it — but it has no caller in this repository and the
# comment saying otherwise was a small, ordinary instance of the defect class
# documented in documents/19.
def enforce_word_budget(text: str, max_words: int = DEFAULT_MAX_DISPLAY_WORDS) -> str:
    """Backwards-compatible wrapper around :func:`enforce_display_budget`."""
    return enforce_display_budget(text, max_words=max_words)


@dataclass
class GlanceabilityCheck:
    """
    Result of the pre-display glanceability gate.

    The gate exists because the decision model turned out to be the wrong tool
    here: it scores *concreteness* well (AUC 0.838 against a 25-line regex's
    0.688) but is completely blind to fact burial, which is the failure the
    wearer actually notices. A cheap regex catches referent-lead and hedged
    summaries; the model is better used offline, after display, as a logging
    judge that eventually earns real calibration data.

    Cost is ~25 lines of arithmetic and zero latency, which is what makes it
    suitable as a *blocking* check on the display path.
    """

    ok: bool
    reason: str
    score: float
    features: dict

    def why(self) -> str:
        return (
            f"{self.reason} (score={self.score:.2f}, "
            + " ".join(f"{k}={v:g}" for k, v in self.features.items())
            + ")"
        )


# Words that are concrete on their own and start a summary usefully.
_CONCRETE_LEXICON = frozenset("""
monday tuesday wednesday thursday friday saturday sunday today tonight
now tomorrow yesterday morning afternoon evening night urgent deadline expires
expiry failed failing error errors red green passed failing blocked stuck
minutes minute hours hour days day seconds second booked scheduled cancelled
""".split())

_TIME_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{1,3}\s*(?:minutes?|mins?|hours?|hrs?|days?)\b"
    r"|\b(?:now|here|inside|today|tonight|tomorrow|yesterday|monday|tuesday"
    r"|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
# A capitalised token is treated as a name unless it is a word that merely
# starts a sentence or clause. The previous pattern excluded ALL
# sentence-initial capitals, which threw away the single most glanceable case:
# a line that leads with the name of the thing -- "Prince Hamlet seeks...",
# "Sunlight scatters...". Those are exactly the lines a wearer reads fastest.
_LEADING_NON_NAMES = frozenset("""
the a an this that these those it its they them he she we you i there here
why how what when where which who whom is are was were do does did can could
will would should may might must have has had been being and but or so then
if but as at by for from in into of on to with without within over under
one two three four five six seven eight nine ten first second next last
every each some any all both more most less least very just also only
""".split())

_NAME_TOKEN = r"[A-Z][a-z]{2,}"


def _is_name(token: str) -> bool:
    return token.lower() not in _LEADING_NON_NAMES


_PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][a-z]{2,}\b(?!\s*[,.]\s+[a-z])"
)
_CONCRETE_TOKEN_RE = re.compile(
    r"\b\d[\d,.:]*\b|" + "|".join(sorted(_CONCRETE_LEXICON))
)


def glanceability_features(text: str) -> dict:
    """
    Cheap features for "can this be understood in one glance".

    Four, exactly as the baseline was specified before any model was run, so
    the numbers stay reproducible:

    * ``digit``     — contains a digit
    * ``proper``    — contains a capitalised non-sentence-initial word
    * ``time``      — matches a clock time or relative-time expression
    * ``lead``      — words before the first concrete token, capped at 6
    """
    clean = strip_display_noise(text)
    if not clean:
        return {"digit": 0.0, "proper": 0.0, "time": 0.0, "lead": 6.0}

    digit = 1.0 if re.search(r"\d", clean) else 0.0
    time = 1.0 if _TIME_RE.search(clean) else 0.0

    # `lead` used to look only for digits and a 30-word lexicon, so on real
    # model output it was pinned at its 6.0 cap almost every time -- which
    # made the score unsatisfiable and the gate fire on nearly everything.
    # A capitalised name counts as concrete too, so leading with the thing
    # itself reads as glanceable.
    lead = 6.0
    match = None
    for m in _CONCRETE_TOKEN_RE.finditer(clean):
        tok = m.group(0)
        if m.group(0)[:1].isupper() and not _is_name(tok):
            continue
        match = m
        break
    if match is None:
        m2 = _PROPER_NOUN_RE.search(clean)
        if m2 and _is_name(m2.group(0)):
            match = m2
    if match is not None:
        lead = min(6.0, float(len(clean[: match.start()].split())))

    proper = 1.0 if any(
        _is_name(t) for t in re.findall(r"\b[A-Z][a-z]{2,}\b", clean)
    ) else 0.0

    return {"digit": digit, "proper": proper, "time": time, "lead": lead}


def glanceability_score(text: str) -> float:
    """
    Baseline glanceability score.

    +1 digit, +1 proper noun, +1 time, minus the fraction of words spent before
    the first concrete token. Tuned by nobody; this is the reference the
    decision model had to beat.
    """
    f = glanceability_features(text)
    return f["digit"] + f["proper"] + f["time"] - min(f["lead"], 6.0) / 6.0


# Hedged framing. A digit inside one of these is a case number, not a fact --
# "Regarding case 4471, there are some points to discuss later" is concrete by
# every count and says nothing.
_HEDGE_RE = re.compile(
    r"\b(?:later|eventually|sometime|when convenient|at some point|"
    r"points? to discuss|to discuss|to consider|consider reviewing|"
    r"there (?:are|is) some|some (?:points?|issues?|things?|updates?)|"
    r"you may|you might|may want to|might want to|keep in mind)\b",
    re.IGNORECASE,
)

# Below this the summary is judged not glanceable and gets repaired or dropped.
GLANCEABILITY_MIN = 0.5


def check_glanceable(text: str, *, minimum: float = GLANCEABILITY_MIN) -> GlanceabilityCheck:
    """
    Blocking pre-display check.

    Two failure modes are cheap to detect and expensive to show:

    * a summary opening with a bare referent ("This was fixed.") never puts the
      fact anywhere the eye lands;
    * a summary with no digit, name or time in it is all connective tissue.

    Fact burial is **not** detectable here — neither judge catches it — which
    is why :func:`repair_buried_fact` exists.
    """
    f = glanceability_features(text)
    score = glanceability_score(text)
    clean = strip_display_noise(text)

    if not clean:
        return GlanceabilityCheck(False, "empty", score, f)

    if f["lead"] >= 6.0 and not f["digit"] and not f["time"]:
        return GlanceabilityCheck(
            False, "no concrete token in the first six words", score, f
        )

    words = clean.split()
    if words and words[0].lower() in {"this", "it", "that", "they", "he", "she"}:
        return GlanceabilityCheck(
            False, "opens with a bare referent", score, f
        )

    if _HEDGE_RE.search(clean):
        return GlanceabilityCheck(
            False, "hedged framing with no concrete claim", score, f
        )

    if score < minimum:
        return GlanceabilityCheck(False, "no concrete anchor", score, f)

    return GlanceabilityCheck(True, "ok", score, f)


def repair_buried_fact(text: str) -> str:
    """
    Move the clause carrying the concrete token to the front.

    Deterministic, no model, and it addresses exactly the failure both judges
    are blind to: the fact is present but rendered in the last line, outside
    the first ~48 characters the wearer actually resolves in a glance.
    """
    clean = strip_display_noise(text)
    if not clean:
        return clean

    # Split into clause-ish units on sentence and clause boundaries.
    clauses = [c.strip() for c in re.split(r"(?<=[.!?;])\s+|\s+[-,]\s+", clean) if c.strip()]
    if len(clauses) < 2:
        return clean

    scored = []
    for index, clause in enumerate(clauses):
        # A real number beats a vague lexicon word. Without this weighting,
        # "The migration is scheduled." (which contains the lexicon word
        # "scheduled") outranks "API keys rotate 2026-11-01", and the reorder
        # silently does nothing.
        has_digit = 2.0 if re.search(r"\d", clause) else 0.0
        has_concrete = 1.0 if _CONCRETE_TOKEN_RE.search(clause) else 0.0
        has_time = 1.0 if _TIME_RE.search(clause) else 0.0
        lead = min(6.0, float(len(clause.split()))) / 6.0
        scored.append((has_digit + has_concrete + has_time - 0.15 * lead,
                       -index, clause))

    best_score = max(s for s, _, _ in scored)
    if best_score <= 0:
        return clean  # nothing concrete anywhere; nothing to reorder toward

    # Highest score first; original order breaks ties. Sorting on score (not on
    # index) is the whole point -- an earlier version preserved the original
    # order here and silently did nothing.
    reordered = [c for _, _, c in sorted(scored, key=lambda t: (-t[0], -t[1]))]
    joined = " ".join(reordered).rstrip(".")
    if not joined:
        return clean

    # Respect the display budget after reordering.
    return enforce_display_budget(joined)


def word_count(text: str | None) -> int:
    return len((text or "").split())


# ---------------------------------------------------------------------------
# Transport
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

    Tries ``max_completion_tokens`` first and falls back to the legacy
    ``max_tokens``, because a number of local OpenAI-compatible servers only
    accept the older name.
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
# Stage 1 / Stage 2
# ---------------------------------------------------------------------------

async def generate_primary_response(
    user_query: str,
    *,
    client=None,
    config: LLMConfig | None = None,
    mode: str = "primary",
    context: str = "",
) -> str:
    """
    STAGE 1 — the heavy thinker.

    ``mode="primary"``  answers a fresh question.
    ``mode="followup"`` continues one the wearer has already seen a summary
    of, using ``context`` to avoid repeating what is on the lens.

    Returns a full, unconstrained passage. Length limiting is Stage 2's job.

    Nothing about the wearer is injected here. This used to append
    "Wearer's known interests: ..." to the primary system prompt, which was
    the most dangerous of the removed injection sites -- stage 2 can only
    re-order facts the answer already contains, whereas stage 1 decides what
    gets reasoned about in the first place. It was removed for consistency
    with the measurement in documents/18 rather than by a separate
    experiment of its own; that is the weaker claim of the two, and it is
    made explicitly.
    """
    config = config or build_llm_config()
    query = (user_query or "").strip()
    if not query:
        raise LLMStageError("empty query")

    client = client or make_client(config)

    system = FOLLOWUP_SYSTEM_PROMPT if mode == "followup" else PRIMARY_SYSTEM_PROMPT

    if mode == "followup":
        user = (
            f"<original_question>\n{query}\n</original_question>\n\n"
            f"<already_shown>\n{context or '(nothing yet)'}\n</already_shown>\n\n"
            "Write only what comes next."
        )
    else:
        user = query

    try:
        text = await _chat(
            client,
            model=config.primary_model or "",
            system=system,
            user=user,
            max_tokens=1200,
            temperature=0.4 if mode == "primary" else 0.5,
        )
    except Exception as exc:
        raise LLMStageError(f"stage 1 ({config.primary_model}) failed: {exc}") from exc

    if not text.strip():
        raise LLMStageError("stage 1 returned an empty response")
    return text.strip()


async def answer_directly(
    query: str,
    *,
    client=None,
    config: LLMConfig | None = None,
    context: str = "",
) -> str:
    """
    STAGE 1 ONLY — answer straight into the display format.

    One call instead of two. The 120-character budget is stated as a hard
    constraint in the system prompt rather than enforced by a second model
    pass, which means the enforcement layer below is still applied: a
    language model is not a physical limit.

    Measured 9-13x faster than the two-stage shape at identical answer
    relevance on a 20-item ground-truth corpus
    (``experiments/fabrication.py``). The trade is that nothing structurally
    separates reasoning from formatting, so an invented number can reach the
    lenses. Whether that trade is worth making is what that experiment is for;
    this exists so the decision is reversible at runtime.
    """
    config = config or build_llm_config()
    client = client or make_client(config)

    system = ONE_STAGE_SYSTEM
    if context:
        system += f"\n\nEarlier in this thread:\n{context}"

    try:
        text = await _chat(
            client,
            model=config.primary_model or config.compressor_model or "",
            system=system,
            user=query,
            max_tokens=120,
            temperature=0.2,
        )
    except Exception as exc:
        raise LLMStageError(f"direct answer ({config.primary_model}) failed: {exc}") from exc

    if not text.strip():
        raise LLMStageError("direct answer returned an empty response")
    return text.strip()


async def condense_for_glasses(
    detailed_text: str,
    *,
    client=None,
    config: LLMConfig | None = None,
) -> str:
    """
    STAGE 2 — the waveguide micro-compressor.

    A structural filter, not a summariser. The display's limits are given as
    hard constraints and the result is still passed through
    :func:`enforce_display_budget`, because a physical limit is not something to
    take on faith from a language model.

    Takes no wearer context. It used to accept a
    :class:`memory.WearerContext` and append an interest block whenever the
    answer mentioned a listed topic -- a gate real enough to be worth writing
    a gating comment about, and which still failed to be worth having.
    documents/18 measures why; the template text survives only in
    ``experiments/personalisation_ab.py``.
    """
    config = config or build_llm_config()
    source = (detailed_text or "").strip()
    if not source:
        raise LLMStageError("nothing to condense")

    client = client or make_client(config)

    system = COMPRESSOR_SYSTEM_PROMPT

    try:
        text = await _chat(
            client,
            model=config.compressor_model or config.primary_model or "",
            system=system,
            user=(
                "Compress this answer into the glasses display format.\n\n"
                f"<answer>\n{source}\n</answer>"
            ),
            max_tokens=120,
            temperature=0.2,
        )
    except Exception as exc:
        raise LLMStageError(f"stage 2 ({config.compressor_model}) failed: {exc}") from exc

    if not text.strip():
        raise LLMStageError("stage 2 returned an empty response")

    return finalize_display_text(text, config, origin="stage 2")


def finalize_display_text(
    text: str,
    config: LLMConfig,
    *,
    origin: str = "answer",
) -> str:
    """
    Everything that must happen to text before it reaches the lenses.

    This used to be inlined in ``condense_for_glasses``, which is the bug this
    function exists to fix. When the architecture flipped to one call by
    default (d646cfd), the glanceability gate went with it: it lived inside
    the stage-2 compressor, stage 2 stopped running by default, and so the
    gate stopped running by default. Nothing broke, no test failed, and the
    README kept advertising "stage 1 -> stage 2 + glanceability gate" as the
    pipeline shape. The gate had simply stopped being part of the pipeline.

    A measured mechanism that only runs on the non-default path is not
    load-bearing. That is the whole finding, and it is why the check is here
    rather than in a caller: every path that produces display text now goes
    through the same three steps, so a future architecture change cannot
    quietly drop one of them again.

    Order matters. The budget is enforced FIRST, because it is a physical
    limit and a repair that pushes text back over the line would be a repair
    that breaks the constraint. The glanceability check runs on the text that
    will actually be shown, not on the text the model produced.
    """
    final = enforce_display_budget(
        text,
        max_chars=config.max_display_chars,
        max_words=config.max_display_words,
    )

    # Pre-display glanceability check. On failure we REPAIR rather than
    # suppress: suppression is the most expensive thing this system can do,
    # and the cheap deterministic repair handles the case both the regex and
    # the decision model are blind to (fact present, rendered last).
    check = check_glanceable(final)
    if not check.ok:
        repaired = repair_buried_fact(final)
        if repaired and check_glanceable(repaired).ok:
            log.info("%s repaired: %s -> %r", origin, check.reason, repaired)
            final = repaired
        else:
            # Still weak: keep it, but say so, so the caller can log it as a
            # candidate for the review queue rather than silently degrading.
            log.warning("%s output not glanceable: %s", origin, check.why())
            final = repaired or final

    return final


# ---------------------------------------------------------------------------
# The handoff
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """Outcome of one pipeline run."""

    query: str
    stage1: str | None
    stage2: str | None
    display_text: str
    stage1_error: str | None = None
    stage2_error: str | None = None


SendFn = Callable[[str], Awaitable]


ONE_STAGE_SYSTEM = (
    "You are the display formatting engine for a head-up display built into "
    "smart glasses. Answer directly in the text that will be rendered on the "
    "lenses.\n\n"
    "HARD CONSTRAINTS:\n"
    f"1. NEVER exceed {DEFAULT_MAX_DISPLAY_CHARS} characters. This is a hard "
    "physical cap.\n"
    "2. Lead with the single most important fact. Keep numbers, names, units "
    "and error codes; drop connective prose.\n"
    "3. No preamble, no markdown, no emoji, no restating the question.\n"
    "4. You must still reason correctly before answering -- think first, then "
    "emit only the final line.\n\n"
    "Output the display text and nothing else."
)

def _lens_failure_line(error: str | None) -> str:
    """
    What the lenses show when stage 1 produced nothing at all.

    Short, fixed, and free of repr punctuation. The wearer cannot act on a
    stack-shaped string, and every character spent on one is a character not
    spent on their actual question. The specific cause still reaches the
    terminal via the caller's log line, which is the audience that can use it.
    """
    text = (error or "").lower()
    if "401" in text or "auth" in text or "api key" in text:
        return "No answer. Model auth failed."
    if "404" in text or "not found" in text:
        return "No answer. Model not found."
    if "timeout" in text or "timed out" in text:
        return "No answer. Model timed out."
    if "connect" in text or "refused" in text or "unreachable" in text:
        return "No answer. Model unreachable."
    return "No answer from model."


async def run_query(
    query: str,
    send: SendFn,
    *,
    client=None,
    config: LLMConfig | None = None,
    mode: str = "primary",
    context: str = "",
    verbose: bool = True,
) -> QueryResult:
    """
    THE HANDOFF: query -> stage 1 -> stage 2 -> BLE write.

        query -> [Stage 1] -> [Stage 2] -> send()

    Failures are contained. A stage-2 failure falls back to deterministic
    local truncation of stage 1; a stage-1 failure falls back to a short
    static line. ``send`` always runs, so the display always receives
    something and an upstream API error can never tear down the session.

    This function used to take a ``store`` and read the wearer's interests
    out of it before either model ran, dispatching the scan to a thread
    because at 8000 events it costs ~26ms of heartbeat. That argument has
    gone with the injection it fed: it was the measured cost of a feature
    that did not earn its place, and it was paid on every single query.
    Nothing here reads the ledger before the answer now, which also means a
    slow query cannot be blamed on a slow store.
    """
    config = config or build_llm_config()
    client = client or make_client(config)
    config = await ensure_models(config, client)

    result = QueryResult(query=query, stage1=None, stage2=None, display_text="")

    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    if config.two_stage:
        say("[1/2] thinking ...")
        try:
            result.stage1 = await generate_primary_response(
                query, client=client, config=config, mode=mode,
                context=context,
            )
            say(f"      {word_count(result.stage1)} words")
        except LLMStageError as exc:
            result.stage1_error = str(exc)
            say(f"      stage 1 FAILED: {exc}")

        if result.stage1:
            say("[2/2] condensing for waveguide ...")
            try:
                result.stage2 = await condense_for_glasses(
                    result.stage1, client=client, config=config,
                )
                say(f"      {word_count(result.stage2)} words")
            except LLMStageError as exc:
                result.stage2_error = str(exc)
                say(f"      stage 2 FAILED: {exc}")
    else:
        # One call. In this mode stage1 IS the display text; there is no
        # separate reasoning pass, so nothing downstream may assume it is a
        # long-form answer.
        say("[1/1] answering ...")
        try:
            result.stage1 = await answer_directly(
                query, client=client, config=config, context=context,
            )
            say(f"      {word_count(result.stage1)} words")
        except LLMStageError as exc:
            result.stage1_error = str(exc)
            say(f"      FAILED: {exc}")

    if result.stage2:
        result.display_text = result.stage2
    elif result.stage1:
        # This branch is the NORMAL path in one-call mode, not a fallback:
        # stage1 IS the display text there. It used to apply only the
        # character budget, which meant the default architecture skipped the
        # glanceability gate entirely -- see finalize_display_text.
        result.display_text = finalize_display_text(
            result.stage1, config, origin="answer")
        if config.two_stage:
            say("      (using locally truncated stage 1 — compressor unavailable)")
    else:
        # The lens gets a short, fixed line. The previous behaviour put the
        # raw exception on the glasses, and a realistic 401 rendered as:
        #
        #     LLM error. stage 1 (m) f
        #     ailed: Error code: 401 -
        #     {'error': {'message':
        #
        # Three of five lines spent on an exception repr, broken mid-word,
        # with the only useful part (401) buried on line two. A wearer walking
        # and glancing gets nothing from any of it. The full diagnostic goes to
        # the terminal, which is where a developer can act on it.
        result.display_text = _lens_failure_line(result.stage1_error)
        say(f"      (no LLM output available) {result.stage1_error or ''}")

    if not result.display_text.strip():
        result.display_text = "No response."

    say(f"[ble] writing {word_count(result.display_text)} words ...")
    await send(result.display_text)
    say(f"[ble] sent: {result.display_text!r}")

    return result