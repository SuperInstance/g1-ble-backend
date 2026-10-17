"""
policy.py
=========

Contested decisions are options, not verdicts.

Several reviewers reached genuinely different conclusions about the same
question, and none of them had the hardware needed to settle it. Picking a
side now would be an argument dressed as a design.

So every open question is a **declared axis** here:

* an explicit set of options,
* a **deciding metric** named in advance, before the data exists,
* a conservative default that applies when evidence is insufficient.

This follows one rule:

    **Expand at setup, streamline at runtime.**

At install time the axes are *open* and the system calibrates against real
measurements — the gaze walk, the review labels, the queue statistics. At
runtime there is no branching on opinions: one :class:`Policy` object is
resolved, and the hot path reads it without deciding anything.

The point is not flexibility for its own sake. It is that **a disagreement
between agents becomes a measurement to be taken rather than a position to be
defended.** Whoever is right wins automatically, and the losing option stays
one flag away in case it turns out to be right about a different day.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("g1.policy")

# The gaze ratio above which head-up is a clean examination signal. Below it,
# fabricated glances CONTAMINATE labels rather than merely adding noise.
RHO_CLEAN = 1.6

# To *demonstrate* 0.99 precision with 95% confidence and zero false pushes:
#     0.99**n <= 0.05   ->   n >= 299
# Until then the interrupt gate stays on rules. It is not that a learned gate
# is worse; it is that it cannot be *shown* to be better, and an unproven gate
# on the path that costs trust is a bad bet.
CLEAN_PUSHES_TO_TRUST_LEARNING = 299

# The push budget is a CONSTRAINT, not a preference, and it belongs inside the
# acceptance predicate rather than downstream of it.
#
# Before this, the budget existed only as prose in an axis rationale ("4/day
# cap") and was enforced elsewhere in the system. So an axis could resolve to
# `push_enabled=on` on the strength of evidence while the system was already
# out of budget -- evidence and constraint are different kinds of input and
# only the first was reaching the decision.
#
# Form follows RSI-Anything (arXiv:2610.13912) section 2.5, which fixes "the
# task metrics, prediction checks, constraints, feedback allowance, and
# acceptance thresholds" as one specification evaluated together. That paper's
# mechanism does not otherwise transfer here -- every one of its results rests
# on a per-action ground-truth outcome, and this system has none -- but this
# particular discipline is independent of that and correct on its own terms.
MAX_PUSHES_PER_HOUR = 2
MAX_PUSHES_PER_DAY = 4

# Labels needed before any relevance weighting means anything at this volume
# (~30 events/day, one wearer).
MIN_LABELS_FOR_MODEL = 100
MIN_LABELS_FOR_CALIBRATED_MODEL = 220


@dataclass(frozen=True)
class PolicyAxis:
    """One open question, the options, and what settles it."""

    id: str
    question: str
    options: tuple[str, ...]
    default: str          # always the conservative option
    decides: str          # the metric, named before the data exists
    rationale: str
    provenance: str = ""  # who argued what, so nobody has to re-argue it

    def __post_init__(self) -> None:
        if self.default not in self.options:
            raise ValueError(
                f"axis {self.id!r}: default {self.default!r} not in options "
                f"{self.options}"
            )


POLICY_AXES: tuple[PolicyAxis, ...] = (
    PolicyAxis(
        id="examination_source",
        question="What opens the queue and counts as having examined a card?",
        options=("touchbar", "head_up", "both"),
        default="touchbar",
        decides="rho = theta/A_b from the 10-minute no-glance calibration walk",
        rationale=(
            "Head-up is only an examination signal if the firmware tilt "
            "threshold sits well clear of gait bob. Below rho=1.6 fabricated "
            "examinations contaminate labels. A dwell filter also cannot "
            "distinguish looking at the glasses from looking at a sign, so "
            "'examined' is an upper bound at best."
        ),
        provenance="Turn C measured the cliff; Claude and zcode both attacked it",
    ),
    PolicyAxis(
        id="g1_model",
        question="Does a discriminative model suppress noise before queueing?",
        options=("on", "off"),
        default="off",
        decides="queue_worth_rate over >=100 review labels",
        rationale=(
            "Measured at AUC 0.898 for noise-vs-queue-worthy, and errors here "
            "cost one queue slot rather than trust. But the model holds 95% of "
            "VRAM, so the gain must be earned. Claude wanted it cut entirely; "
            "zcode wanted it on. Default is off because it is unproven on real "
            "wearer data, not because it was judged bad."
        ),
        provenance="documents/13 vs documents/14; experiments/results/REPORT-A2",
    ),
    PolicyAxis(
        id="interrupt_gate",
        question="What authorises a push: rules, or learned weights?",
        options=("rules", "learned"),
        default="rules",
        decides="count of clean pushes with 95% confidence",
        rationale=(
            "0.99**n <= 0.05 needs n >= 299 clean pushes, about 75 days at the "
            "4/day cap. A learned gate cannot be shown to meet the target "
            "before then, and a mis-fire here is abandonment risk #1."
        ),
        provenance="Claude's convergence, independently confirmed by zcode",
    ),
    PolicyAxis(
        id="detail_generation",
        question="Is item detail generated when tapped, or precomputed at the desk?",
        options=("off", "on"),
        default="off",
        decides="drill_miss_rate over the first fortnight",
        rationale=(
            "On-demand puts the slowest component on the interaction that gets "
            "abandoned first when it is slow. But if taps routinely find "
            "nothing, the log asks for it back. Default off; the log decides."
        ),
        provenance="zcode proposed deferring DETAIL and logging drill_miss",
    ),
    PolicyAxis(
        id="push_enabled",
        question="Does anything push at all during the first milestone?",
        options=("off", "on"),
        default="off",
        decides="tier labels collected for a full fortnight",
        rationale=(
            "No source is yet trusted at 0.99. Milestone 1 is about learning "
            "which items deserve attention, which the queue teaches and a push "
            "would destroy."
        ),
        provenance="both convergence rounds",
    ),
)

AXES_BY_ID = {a.id: a for a in POLICY_AXES}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """One axis resolved, with the evidence that resolved it."""

    axis: str
    option: str
    reason: str
    evidence: dict = field(default_factory=dict)
    enough_evidence: bool = True
    # True when a human pinned this by hand rather than a metric settling it.
    #
    # Added because `enough_evidence` was doing a job it could not do. An
    # override used to be recorded as Decision(..., True), so `--set-policy
    # push_enabled=on` produced an axis that Policy.unresolved() did not list
    # and that any gate keyed on "is this settled?" would read as MEASURED. It
    # was a hand assertion wearing the clothes of a measurement, and it
    # persists in policy.json, so it would be re-applied by any future
    # unattended process at startup. The canonical failure is a test switch
    # that outlives the test session: someone flips an axis on to see
    # something happen, and three weeks later a daemon is live with it.
    #
    # Overrides are now manual=True and enough_evidence=False. They still
    # take effect -- that is the point of an override -- but they can no
    # longer masquerade as evidence, and `Policy.manual()` names them so a
    # gate that needs a real measurement can refuse one.
    manual: bool = False


def _resolve_examination_source(ev: dict) -> Decision:
    rho = ev.get("rho")
    if rho is None:
        return Decision("examination_source", "touchbar",
                        "no calibration walk yet", ev, False)
    if rho >= RHO_CLEAN:
        return Decision("examination_source", "both",
                        f"rho={rho:.2f} >= {RHO_CLEAN}", ev, True)
    return Decision("examination_source", "touchbar",
                    f"rho={rho:.2f} < {RHO_CLEAN}: fabricated glances "
                    f"contaminate labels", ev, True)


def _resolve_g1_model(ev: dict) -> Decision:
    labels = ev.get("n_labels", 0)
    if labels < MIN_LABELS_FOR_MODEL:
        return Decision("g1_model", "off",
                        f"{labels} labels < {MIN_LABELS_FOR_MODEL}: unproven "
                        f"on real data", ev, False)
    worth = ev.get("queue_worth_rate")
    if worth is None:
        return Decision("g1_model", "off", "no queue_worth_rate yet", ev, False)
    if worth >= 0.5:
        return Decision("g1_model", "on",
                        f"queue_worth_rate={worth:.2f} over {labels} labels",
                        ev, True)
    return Decision("g1_model", "off",
                    f"queue_worth_rate={worth:.2f} below 0.50: the queue was "
                    f"not worth glanceing, so filtering it was pointless",
                    ev, True)


def _resolve_interrupt_gate(ev: dict) -> Decision:
    clean = ev.get("clean_pushes", 0)
    if clean >= CLEAN_PUSHES_TO_TRUST_LEARNING:
        return Decision("interrupt_gate", "learned",
                        f"{clean} clean pushes >= {CLEAN_PUSHES_TO_TRUST_LEARNING}",
                        ev, True)
    return Decision("interrupt_gate", "rules",
                    f"{clean} clean pushes; need "
                    f"{CLEAN_PUSHES_TO_TRUST_LEARNING} to show 0.99 at 95% "
                    f"confidence", ev, clean > 0)


def _resolve_detail_generation(ev: dict) -> Decision:
    rate = ev.get("drill_miss_rate")
    if rate is None:
        return Decision("detail_generation", "off", "no drill log yet", ev, False)
    if rate >= 0.30:
        return Decision("detail_generation", "on",
                        f"drill_miss_rate={rate:.2f}: taps are routinely "
                        f"finding nothing", ev, True)
    return Decision("detail_generation", "off",
                    f"drill_miss_rate={rate:.2f} below 0.30", ev, True)


def _resolve_push_enabled(ev: dict) -> Decision:
    days = ev.get("label_days", 0)

    # Constraint first. Being out of budget is not a matter of evidence, so it
    # is checked before evidence and cannot be overridden by it. Reported as
    # enough_evidence=False so it shows up as UNRESOLVED rather than as a
    # decided axis: the budget is exhausted, not a judgement about trust.
    spent_hour = int(ev.get("pushes_last_hour", 0) or 0)
    spent_day = int(ev.get("pushes_today", 0) or 0)
    if spent_day >= MAX_PUSHES_PER_DAY:
        return Decision("push_enabled", "off",
                        f"{spent_day}/{MAX_PUSHES_PER_DAY} daily pushes used; "
                        f"budget exhausted regardless of evidence", ev, False)
    if spent_hour >= MAX_PUSHES_PER_HOUR:
        return Decision("push_enabled", "off",
                        f"{spent_hour}/{MAX_PUSHES_PER_HOUR} hourly pushes "
                        f"used; budget exhausted regardless of evidence",
                        ev, False)

    if days >= 14:
        return Decision("push_enabled", "on",
                        f"{days} days of tier labels collected", ev, True)
    return Decision("push_enabled", "off",
                    f"{days} days of tier labels; milestone 1 is queue-only",
                    ev, days > 0)


RESOLVERS = {
    "examination_source": _resolve_examination_source,
    "g1_model": _resolve_g1_model,
    "interrupt_gate": _resolve_interrupt_gate,
    "detail_generation": _resolve_detail_generation,
    "push_enabled": _resolve_push_enabled,
}


@dataclass
class Policy:
    """
    The resolved, flat policy the runtime reads.

    There is no branching on opinions in the hot path: the axes were decided at
    setup, and this object is just the answers.
    """

    decisions: dict[str, Decision] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)

    def get(self, axis_id: str) -> str:
        return self.decisions[axis_id].option

    def on(self, axis_id: str) -> bool:
        return self.get(axis_id) == "on"

    def evidence_for(self, axis_id: str) -> Decision:
        return self.decisions[axis_id]

    def unresolved(self) -> list[str]:
        return [d.axis for d in self.decisions.values() if not d.enough_evidence]

    def manual(self) -> list[str]:
        """Axes a human pinned, rather than a metric settling.

        The set that matters for anything irreversible. `--evidence` prints
        it, and any future path that pushes to a person's face should refuse
        an axis that appears here unless the operator passed an explicit
        "I know this is unmeasured" flag -- because the whole failure mode
        of a manual override is that it looks identical to a settled one.
        """
        return [d.axis for d in self.decisions.values() if d.manual]

    def settled_by_evidence(self, axis_id: str) -> bool:
        """
        True only if a metric, not a person, decided this axis.

        Use this, not ``enough_evidence``, for any gate whose failure is
        visible on someone's face.
        """
        d = self.decisions.get(axis_id)
        return bool(d and d.enough_evidence and not d.manual)

    def report(self) -> str:
        lines = []
        for axis in POLICY_AXES:
            d = self.decisions.get(axis.id)
            if d is None:
                continue
            mark = "  " if d.enough_evidence else "??"
            over = "  [override]" if axis.id in self.overrides else ""
            lines.append(
                f"{mark} {axis.id:<20} {d.option:<10} {d.reason}{over}"
            )
        pending = self.unresolved()
        if pending:
            lines.append(
                "\n  ?? = decided conservatively for lack of evidence. "
                "The metric that would settle it is named on each axis."
            )
        return "\n".join(lines)


def resolve(evidence: Optional[dict] = None,
            overrides: Optional[dict[str, str]] = None) -> Policy:
    """
    Turn evidence into a flat policy.

    ``evidence`` is keyed by the metrics named in each axis's ``decides``
    field. An axis whose evidence is missing resolves to its conservative
    default and is marked as unresolved — never guessed.
    """
    evidence = evidence or {}
    overrides = dict(overrides or {})

    decisions: dict[str, Decision] = {}
    for axis in POLICY_AXES:
        axis_evidence = evidence.get(axis.id, {})
        decision = RESOLVERS[axis.id](axis_evidence)
        if axis.id in overrides:
            chosen = overrides[axis.id]
            if chosen not in axis.options:
                raise ValueError(
                    f"{axis.id}: {chosen!r} not in {axis.options}"
                )
            decision = Decision(
                axis.id, chosen,
                f"manual override; would have been "
                f"{decision.option} ({decision.reason})",
                axis_evidence, False, manual=True,
            )
        decisions[axis.id] = decision

    return Policy(decisions=decisions, overrides=overrides)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def policy_path() -> Path:
    return Path.home() / ".local" / "share" / "g1-ble-backend" / "policy.json"


def load(evidence: Optional[dict] = None) -> Policy:
    """
    Resolve from evidence plus any saved manual overrides.

    Expired overrides are dropped here rather than at write time, so an
    override with a TTL stops applying the moment it lapses even if nobody
    re-runs the command that set it. That is the difference between an expiry
    and a reminder.
    """
    overrides: dict[str, str] = {}
    path = policy_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            overrides = _prune_expired(raw.get("overrides", {}),
                                       raw.get("set_at", {}),
                                       raw.get("ttl_s"))
        except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
            log.warning("could not read saved overrides: %s", exc)
    return resolve(evidence, overrides)


def _prune_expired(overrides: dict, set_at: dict, ttl_s) -> dict:
    """Drop overrides whose TTL has passed.

    Malformed records are KEPT rather than dropped. That is the right failure
    direction: a TTL file we cannot parse means we do not know when the pin
    lapses, and silently discarding an operator's override is more surprising
    than honouring one slightly too long.
    """
    if not isinstance(overrides, dict):
        return {}
    if not ttl_s or not isinstance(set_at, dict):
        return dict(overrides)
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        log.warning("unusable override ttl %r; ignoring the expiry", ttl_s)
        return dict(overrides)
    now = time.time()
    alive: dict[str, str] = {}
    for axis_id, option in overrides.items():
        stamp = set_at.get(axis_id)
        if not isinstance(stamp, (int, float)):
            alive[axis_id] = option        # no stamp recorded: keep, as before
            continue
        if (now - stamp) > ttl:
            log.warning("override for %s expired after %.0fs; ignoring",
                        axis_id, ttl)
            continue
        alive[axis_id] = option
    return alive


def save_overrides(overrides: dict[str, str], ttl_s: Optional[float] = None) -> Path:
    """
    Persist manual overrides so they survive restarts.

    ``ttl_s``, when given, stamps each override with an expiry so ``load()``
    stops honouring it. This exists for one specific accident: pinning an
    axis on to watch something happen, forgetting, and finding weeks later
    that a live process has been running on the pin. Without a TTL an
    override is permanent; with one it stops on its own.
    """
    for axis_id, option in overrides.items():
        if axis_id not in AXES_BY_ID:
            raise ValueError(f"unknown axis {axis_id!r}")
        if option not in AXES_BY_ID[axis_id].options:
            raise ValueError(
                f"{axis_id}: {option!r} not in {AXES_BY_ID[axis_id].options}"
            )
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload: dict = {"overrides": overrides}
    if ttl_s:
        payload["set_at"] = {k: now for k in overrides}
        payload["ttl_s"] = ttl_s
    path.write_text(json.dumps(payload, indent=2))
    return path


def describe_axes() -> str:
    """Human-readable list of every open question and what settles it."""
    lines = ["Policy axes — every disagreement is a switch, not a verdict.", ""]
    for a in POLICY_AXES:
        lines.append(f"{a.id}")
        lines.append(f"  question : {a.question}")
        lines.append(f"  options  : {' | '.join(a.options)}")
        lines.append(f"  default  : {a.default}  (conservative until proven)")
        lines.append(f"  decided by: {a.decides}")
        if a.provenance:
            lines.append(f"  from     : {a.provenance}")
        lines.append("")
    return "\n".join(lines)