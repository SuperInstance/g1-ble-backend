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

The honest caveat, from TypeSafe's own docs: *"Treat confidence as how
concentrated an answer is, not a correctness guarantee, and calibrate
thresholds against your own labelled data."* Nothing here assumes calibration
that has not been measured on this corpus.
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


def available() -> bool:
    """True when the SDK is installed and a key is configured."""
    if not os.environ.get("TYPESAFE_API_KEY"):
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
    if not os.environ.get("TYPESAFE_API_KEY"):
        return None
    try:
        from typesafe_sdk import Choice
    except ImportError:
        return None

    import time

    started = time.monotonic()
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
    except Exception:      # noqa: BLE001 -- a gate must never crash delivery
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