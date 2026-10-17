"""
Jev — TypeSafe's System One decision model — as a gate for the G1 layer.

Why this exists. The project's noise-suppression gate has been the weakest
link for a long time. `policy.py` records why, in its own words:

    "0.99**n <= 0.05 needs n >= 299 clean pushes, about 75 days at the
     4/day cap. A learned gate cannot be shown to meet the target before
     then."

That is an argument about *accumulating evidence from a generative model*.
Jev is not a generative model. It returns a **calibrated probability** for a
closed-set question, which means the false-fire probability of a push is
available directly instead of having to be inferred from 75 days of clean
sends. Whether it is actually well calibrated on this project's data is a
separate question, and the only way to know is to measure it -- see
``experiments/jev_gate.py``.

Deliberate properties of this wrapper:

* **Optional.** ``typesafe_sdk`` is imported lazily. Nothing in the core
  imports this module, so the BLE and LLM paths are unaffected when it is not
  installed or not configured.
* **Absent key is not an error.** Returns ``None`` so callers can fall back to
  the existing gate. A gate that crashes when the network is down would be
  worse than no gate.
* **Synchronous.** The BLE event loop must never block on a network call; the
  caller is responsible for dispatching to a thread, exactly as
  ``Session._record`` does for SQLite.

Measured facts about Jev, taken from SuperInstance/AI-Writings
(situations/JEV-USAGE-LOG.md, situations/JEV-FINDINGS.md, 2026-09-29) rather
than from the vendor page. They constrain how this module may be used:

* **~150 ms typical latency**, ~450 input / 40 output tokens per call. That
  fits the delivery path; a 32s link budget is not the constraint.
* **Use ``choice``, not N independent ``noul``s.** Measured: independent nouls
  are not mutually exclusive -- on "most moons" BOTH Jupiter (0.52) and Saturn
  (0.79) cleared 0.5, so a threshold reader accepts a wrong answer. ``choice``
  normalises across options, costs 1 call instead of N, and its confidence
  honestly flagged the hardest item. Reserve per-option ``noul`` for
  genuinely non-exclusive (multi-select) questions.
* **``choice`` needs a ``criteria`` MAP keyed by option.** An ``options``
  array alone returns 422.
* **DO NOT use Jev for character or letter counting.** Two independent
  sources agree: a 32-item competence map scored counting 2/5 (0.40) while
  everything else ran 1.00, and letter-by-letter spelling probes came back
  0.82-0.89 TRUE on deliberate misspellings -- confidently wrong. This project
  has a 120-CHARACTER display budget, which is precisely a character-counting
  task. ``enforce_display_budget`` in pipeline.py is therefore authoritative
  and Jev is explicitly barred from that role.
* **Competent at** arithmetic, dates, geography, units, factual recall, logic,
  geometry and sequences. Relevant here because it makes Jev a legitimate
  INDEPENDENT judge for the fabrication question in
  ``experiments/fabrication.py`` -- which is the measurement that decides
  whether the two-stage pipeline earns its cost.
* Vendor caveat, unretracted: *"Treat confidence as how concentrated an answer
  is, not a correctness guarantee, and calibrate thresholds against your own
  labelled data."*

A note on degradation, learned the hard way elsewhere: the same corpus records
a connector that "random-fallbacked" before the schema arrived -- probabilities
that were "costumes draped over ignorance". This module returns ``None`` on any
failure and never invents a number, for exactly that reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_MODEL = "jev-latest"

# The G1 question, in Jev's vocabulary. This is a closed-set decision, which
# is what the primitive is for: Jev cannot return a label we did not offer, so
# a malformed "worth interrupting = maybe" is not reachable.
WORTH_INTERRUPTING = (
    "A message just arrived. Should it interrupt the wearer right now, "
    "while they are walking and doing something else?"
)

NOT_WORTHY = "not worth interrupting: noise, stale, or already handled"
WORTHY = "worth interrupting: new, specific, and time-sensitive"
UNSURE = "borderline: might matter but could wait"


# Upstream intermittently returns 503/429 (measured: labs/jev-kat retries on
# both). Everything else will not fix itself.
RETRY_BACKOFF_S = (0.25, 0.75, 2.0)

# The ambiguity sentinel. On any failure an interrupt decision must NOT read as
# "definitely not worth it", because 0 means that. The ESP32 cell in
# AI-Writings encodes it explicitly as p=0.5, confidence=0.0 -- "spike = 0.5
# ambiguous". In Python the honest encoding is None, which is falsy but is not
# zero, and `ambiguous_verdict()` below exists so that a caller aggregating
# across sources cannot accidentally turn "unknown" into a confident no.
AMBIGUOUS = 0.5


def _is_transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in ("503", "429", "timeout", "temporar", "overload"))


def ambiguous_verdict(model: str = DEFAULT_MODEL) -> GateVerdict:
    """
    An explicit "we do not know", distinct from "not worth it".

    Never collapse a network failure into a quiet no: on this project that
    would silently suppress notifications the wearer asked for, and the
    suppress path is the one that is supposed to be the most careful.
    """
    return GateVerdict(
        worth_interrupting=AMBIGUOUS,
        confidence=0.0,
        probabilities={},
        model=model,
        latency_ms=0.0,
    )


@dataclass(frozen=True)
class GateVerdict:
    """One gate decision, with the reasoning kept alongside it."""

    worth_interrupting: float        # 0..1 probability of WORTHY
    confidence: float                 # how concentrated the distribution is
    probabilities: dict[str, float]
    model: str
    latency_ms: float

    def fired(self, threshold: float) -> bool:
        return self.worth_interrupting >= threshold


# Two spellings are in circulation. The SDK documents TYPESAFE_API_KEY; every
# live client in SuperInstance/AI-Writings uses TYPESAFEAI_KEY, and the key
# scrub receipt shows the real token shape is "apikey_...". Accept both.
KEY_ENVS = ("TYPESAFE_API_KEY", "TYPESAFEAI_KEY")


def api_key() -> Optional[str]:
    for name in KEY_ENVS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def available() -> bool:
    """True when the SDK is installed and a key is configured."""
    if not api_key():
        return False
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _client():
    from typesafe_sdk import TypeSafeClient
    return TypeSafeClient()


def worth_interrupting(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    client=None,
    timeout: float = 5.0,
) -> Optional[GateVerdict]:
    """
    Ask Jev whether ``text`` deserves an interrupt. None on any failure.

    ``timeout`` is deliberately short and independent of the LLM timeout: this
    sits on the delivery path, and a slow answer is worth less than the
    deterministic fallback it replaces.
    """
    if not api_key():
        return None
    try:
        from typesafe_sdk import Choice
    except ImportError:
        return None

    import time

    started = time.monotonic()
    response = None
    for attempt in range(len(RETRY_BACKOFF_S) + 1):
        try:
            client = client or _client()
            response = client.system_one(
                state={"message": text},
                questions={
                    "interrupt": Choice(
                        instructions=WORTH_INTERRUPTING,
                        criteria={
                            "worthy": WORTHY,
                            "not_worthy": NOT_WORTHY,
                            "borderline": UNSURE,
                        },
                    ),
                },
                model=model,
                timeout=timeout,
            )
            break
        except Exception as exc:      # noqa: BLE001
            # 503/429 are documented as intermittent upstream, and one client in
            # AI-Writings counts every retry as a finding rather than hiding it.
            # Anything else (401, schema error) will not fix itself, so stop.
            if not _is_transient(exc) or attempt == len(RETRY_BACKOFF_S):
                return None
            time.sleep(RETRY_BACKOFF_S[attempt])
    if response is None:
        return None

    elapsed = (time.monotonic() - started) * 1000.0
    try:
        answer = response.choices["interrupt"]
        probs = dict(getattr(answer, "probabilities", {}) or {})
    except Exception:      # noqa: BLE001
        return None

    return GateVerdict(
        worth_interrupting=float(probs.get("worthy", 0.0)),
        confidence=float(getattr(answer, "confidence", 0.0) or 0.0),
        probabilities=probs,
        model=str(getattr(response, "model", model)),
        latency_ms=elapsed,
    )