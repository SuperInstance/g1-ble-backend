"""
wearer.py — the validation SUBJECT, written to disagree with the system.

Not part of the G1 relay agent. It is the thing the agent is measured against,
written by someone who read DESIGN.md but not the implementation, then written
*adversarially* to it. If the agent scores well against this wearer that is weak
evidence: the two do not share an author, and this one is built to punish the
specific assumptions the agent makes.

Ground rules that keep it a fair experiment rather than a rigged one:

* **Events only.** Every method takes what a wearer could actually observe: an
  item, or the text that was rendered. Nothing imports policy, memory or any
  scorer, and nothing reads the system's internal state.
* **Hidden state stays hidden.** Interest weights live in ``_weights`` and are
  reachable only through :meth:`report`, which is for printing after the fact.
  No decision path reads them back out.
* **Deterministic.** One RNG seeded by ``seed``: same seed plus same call
  sequence gives the same wearer, so a failure is reproducible, not a story.

The design's thesis is that a mis-fire costs trust and a correct push earns it.
This wearer takes that seriously and makes it sharper: bad interruptions
compound superlinearly, trust does not recover on a 24-hour schedule, and enough
of them end the experiment by abandonment rather than by a metric.
"""

from __future__ import annotations

import math
import random
import re
from typing import Any, Optional

# The lens is 5 lines of 24 characters. What a person can actually resolve in
# one glance is the first two lines, so ~48 characters is the fovea. A concrete
# token ("14:30", "4821", "urgent") has to land inside it or the wearer sees
# prose, not information.
HEAD_CHARS = 48
LINE_CHARS = 24
DISPLAY_CAP_MS = 8000          # head-down release timeout
MS_PER_CHAR_EASY = 26          # reading a line you can parse
MS_PER_CHAR_HARD = 44          # scanning something you cannot

# Coarse concreteness test, written independently of the relay's own lexicon:
# does this text contain something checkable -- a number, a clock time, or a
# word that names a state change?
_CONCRETE_RE = re.compile(
    r"\d"                                   # any number: ids, counts, times
    r"|\b(?:now|today|tonight|tomorrow|urgent|immediately|due|deadline|expire"
    r"|expires|blocked|failed|failing|down|offline|cancell?ed|done|ready"
    r"|waiting|approved|rejected|accepted|final|finali[sz]ed)\b",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")

DEFAULTS: dict[str, Any] = {
    # Hidden interests: printed only, never consulted by the harness.
    "subjects": ["g1-protocol", "glasses-mtu", "deploy", "outage",
                 "attention-design", "meeting", "battery", "digest",
                 "social", "marketing"],
    "threshold": 1.0,             # utility an item must beat (scale is ~0..2)
    "noise_sigma": 0.13,          # irreducible indecision
    "curiosity_rate": 0.035,      # sometimes I look at what I do not care about
    # --- interruption cost: the product thesis lives here. These are
    # multipliers on the BAR an item must clear, not on attention itself. The
    # ceiling matters: without it fatigue x evening cost reaches ~14x and the
    # wearer goes completely mute, which would flatter any system under test
    # (nothing gets through, so precision looks perfect). Capped, marginal
    # items still die first while the single most urgent thing gets through.
    "fatigue_gain": 0.18,         # added per *bad* interruption
    "fatigue_exponent": 1.6,      # >1: the 4th bad costs far more than the 3rd
    "fatigue_cap": 2.6,
    "cost_ceiling": 2.0,          # max bar an item ever has to clear
    "fatigue_half_life_h": 2.5,   # badness fades, but slowly
    "day_cost_start": 0.72,       # 08:00: interruptions are cheap and expected
    "day_cost_end": 2.05,         # 19:00: re-entry is expensive, tolerance thin
    # --- trust, and the end of the experiment ---
    "trust_decay_bad": 0.21,
    "trust_gain_good": 0.055,
    "trust_drift_per_day": 0.03,
    # --- diminishing returns within a topic ---
    "repeat_base": 0.74,          # each re-sighting is worth this fraction
    "expand_satiation": 0.45,     # each *expansion* costs this much more
    "recency_recovery": 0.32,     # a boring subject is interesting again after a day
    # --- the already-knew case ---
    "known_penalty": 0.45,        # utility removed when I already have this
    "redundancy_compound": 0.30,  # each restatement adds to the annoyance
    "recognition_miss_rate": 0.22,  # I do NOT notice redundancy straight away
    # --- glance / gaze ---
    "notice_base": 0.62,          # do I even register a peripheral head-up cue
    "notice_habituation": 0.11,   # per head-up: the cue stops meaning anything
    "notice_floor": 0.12,
    "re_examine_gain": 0.62,
}


class VirtualWearer:
    """One person wearing the glasses for a fortnight."""

    def __init__(self, seed: int, config: Optional[dict] = None) -> None:
        self.seed = seed
        self.cfg = {**DEFAULTS, **(config or {})}
        self.rng = random.Random(seed)

        # ---- hidden interests (drift; see _drift) -------------------------
        subjects: list[str] = list(self.cfg["subjects"])
        # Start uneven: nobody cares equally about everything on day 1.
        self._weights: dict[str, float] = {
            s: round(self.rng.uniform(0.18, 0.72), 4) for s in subjects
        }
        self._regime_shift_day: int = int(self.cfg.get("regime_shift_day", 6))
        self._drift_log: list[dict] = []
        self._last_drift_day: int = -1
        self._focus_shift_day: int = -1  # set when the drift fires

        # ---- clock, driven by observed event timestamps -------------------
        self._minutes: float = 8 * 60      # start of a working day
        self._day: int = 0                 # 0..13, the fortnight
        self._day_state: str = "active"    # active | headache | abandoned
        self._epoch_day: Optional[int] = None  # day index of the first event seen

        # ---- attention and trust ------------------------------------------
        # Clock-times of recent bad interruptions. Ages must be computed against
        # the clock and never frozen at insertion: irritation genuinely fades
        # over a quiet hour, and a model that cannot forget escalates to "never
        # interrupt" by lunchtime.
        self._bad: list[float] = []
        self._pending: list[dict] = []      # interrupts never answered (resentment)
        self._fragments: float = 0.0        # attention lost, counted even if I
                                            # never get to judge the item
        self._trust: float = 1.0
        self._abandoned_day: Optional[int] = None

        # ---- what I have already been told, and what I have seen ----------
        self._known_topics: dict[str, int] = {}     # topic -> times laptop told me
        self._topic_hist: dict[str, dict] = {}      # topic -> seen/expands/last
        self._lens: dict[str, dict] = {}            # display_text -> legible/value

        # ---- counters for report() ----------------------------------------
        self._n_seen = self._n_shown = self._n_interrupts = 0
        self._n_bad = self._n_redundant = 0
        self._n_examined = self._n_reexamined = self._n_unreadable_reeads = 0
        self._headups = 0

    # --- clock and drift ---
    def advance(self, minutes: float) -> None:
        """Move the wearer forward. The harness owns real time; this is it."""
        self._minutes += float(minutes)
        while self._minutes >= 24 * 60:
            self._minutes -= 24 * 60
            self._day += 1
            self._trust = min(1.0, self._trust + self.cfg["trust_drift_per_day"])
        self._age_pending()
        self._drift()

    def _age_pending(self) -> None:
        """
        Interrupts that were never resolved.

        Real resentment is not a verdict, it is a residue: if something pulled
        me out of a thought and I never found out what it was, it teaches me
        only that whatever does this is not to be trusted. So an unanswered
        interrupt converts into fatigue purely by ageing -- the trap for any
        system that fires and moves on, where the cost is paid in uncertainty
        rather than in disappointment.
        """
        now = self._clock_hours()
        keep: list[dict] = []
        for p in self._pending:
            age = now - p["t"]
            if age < 0.25:                       # still fresh; give it a chance
                keep.append(p)
                continue
            self._fragments = max(0.0, self._fragments - 0.1)
            if self.rng.random() < 0.55:          # it lingers
                self._register_bad(known=False, extra_trust=0.05)
        self._pending = keep

    def _sync(self, item: dict) -> None:
        """
        Let an event's own timestamp drive the clock, if it carries one.

        Timestamps are part of the event stream, so reading them is still
        event-driven -- and it matters more than it looks. A harness that never
        advances the wearer leaves every decision frozen at 08:00 of day 0,
        which silently disables the time-of-day cost, the daily trust drift and
        the day-6 regime shift. Handles epoch seconds and the ISO strings the
        spool format actually uses.
        """
        ts = item.get("ts")
        if ts is None:
            return
        day, minute = None, None
        if isinstance(ts, (int, float)):
            if ts < 10_000_000:            # plainly a clock-of-day in minutes
                day, minute = self._day, int(ts)
            else:
                day, minute = divmod(int(ts) // 60, 24 * 60)
        elif isinstance(ts, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts)
                base = datetime(1970, 1, 1)
                delta = dt.replace(tzinfo=None) - base
                total = int(delta.total_seconds() // 60)
                day, minute = divmod(total, 24 * 60)
            except ValueError:
                return
        if minute is None or day is None:
            return
        # Day 0 is whatever day the first event arrived on, so the fortnight
        # counts from the start of observed wear rather than from 1970.
        if self._epoch_day is None:
            self._epoch_day = day
        self._minutes = float(minute)
        self._day = max(self._day, day - self._epoch_day)
        self._age_pending()
        self._drift()

    def _drift(self) -> None:
        """
        Slow random walk plus one regime shift around day 6.

        Real interest wanders a little each day and occasionally a new project
        swallows the week. The shift breaks any system that quietly memorised
        "this person cares about X" on day 1 and never checks again.
        """
        if self._day == self._last_drift_day:
            return
        self._last_drift_day = self._day
        for s in self._weights:
            step = self.rng.gauss(0.0, 0.035)
            # Mean-reverting, so nothing drifts to 0 or 1 over a fortnight.
            step += 0.10 * (0.5 - self._weights[s])
            self._weights[s] = round(min(1.0, max(0.0, self._weights[s] + step)), 4)
        if self._day >= self._regime_shift_day and self._focus_shift_day < 0:
            self._focus_shift_day = self._day
            ranked = sorted(self._weights, key=lambda s: -self._weights[s])
            if len(ranked) >= 2:
                # Work focus moves: the old subject is not deleted, it is
                # demoted, which is the harder case for a decaying-memory model.
                self._weights[ranked[0]] = round(self._weights[ranked[0]] * 0.45, 4)
            for s in self._weights:
                self._weights[s] = round(min(1.0, self._weights[s] + 0.18), 4)
        self._drift_log.append(
            {"day": self._day, "minute": self._minutes,
             "weights": dict(self._weights)}
        )

    # --- derived state ---
    @property
    def hour(self) -> float:
        return self._minutes / 60.0

    def _relevance(self, item: dict) -> float:
        """Latent interest in this item's subject(s). Hidden state, used only
        to produce reactions -- never visible to the harness."""
        topics = self._topics(item)
        if not topics:
            return 0.30  # an item I cannot place: mild background interest
        return sum(self._weights.get(t, 0.35) for t in topics) / len(topics)

    def _topics(self, item: dict) -> list[str]:
        """
        Which subject(s) this item is about.

        Accepts every shape the repo's own spool format uses, because a topic
        the wearer cannot extract is a topic whose hidden weight never applies
        -- and an inert weight model would make the whole exercise measure
        nothing. ``as_spool_json()`` carries ``subject`` and ``keywords`` but no
        ``topic``, so those are fallbacks rather than extras.
        """
        t = item.get("topics") or item.get("topic")
        if isinstance(t, str):
            return [t]
        if isinstance(t, (list, tuple)):
            return [str(x) for x in t]
        subject = item.get("subject")
        if isinstance(subject, str) and subject:
            return [subject]
        kws = item.get("keywords")
        if isinstance(kws, (list, tuple)) and kws:
            return [str(x) for x in kws]
        return []

    def _urgency(self, item: dict) -> float:
        """0..1, from the item as presented -- these are fields a wearer can
        perceive, not system internals. The spool format calls it ``cls``."""
        u = item.get("urgency")
        if isinstance(u, (int, float)):
            return max(0.0, min(1.0, float(u)))
        cls = str(item.get("class", item.get("cls", "")))
        if cls in ("blocked_job", "meeting", "device"):
            return 0.75
        if cls in ("ci", "build"):
            return 0.35
        if cls in ("social", "noise"):
            return 0.05
        text = f"{item.get('line', '')} {item.get('summary', '')}"
        if _CONCRETE_RE.search(text):
            return 0.4
        return 0.1

    def _legible(self, display_text: str) -> bool:
        """Is the actual fact inside the part of the lens a person can read in
        one look? This is the whole G3 claim, restated as a human constraint."""
        if not display_text:
            return False
        head = str(display_text)[:HEAD_CHARS]
        return bool(_CONCRETE_RE.search(head))

    def _truncated(self, display_text: str) -> bool:
        """Display budget is 120 characters. Text cut off mid-fact forces a
        second look, or an angry dismissal."""
        return len(str(display_text or "")) > 120

    def _day_cost(self) -> float:
        """
        Time-of-day cost: cheap at 08:00, expensive by evening.

        Not flat and not linear -- a morning interruption is a nuisance, an
        evening one is what makes you take the glasses off. The lunch dip is
        real too: I *expect* interruptions then.
        """
        h = self.hour
        if h < 8 or h > 21:
            return 1.2  # not working: near-random tolerance
        t = (h - 8.0) / 11.0
        cost = self.cfg["day_cost_start"] + (self.cfg["day_cost_end"] - self.cfg["day_cost_start"]) * t
        # lunch dip
        if 12.0 <= h <= 13.5:
            cost *= 0.72
        return cost

    def _fatigue_cost(self) -> float:
        """
        Superlinear escalation after bad interruptions.

        The product thesis as arithmetic: cost after n recent bad interruptions
        grows as n^1.6, so the 4th costs far more than the 3rd. Badness decays
        over hours, so a quiet lunch genuinely restores some budget.
        """
        now = self._clock_hours()
        live = 0.0
        for t in self._bad:
            live += math.pow(0.5, max(0.0, now - t) / self.cfg["fatigue_half_life_h"])
        self._bad = [t for t in self._bad if now - t < 48.0]  # stop holding grudges forever
        mult = 1.0 + self.cfg["fatigue_gain"] * math.pow(live, self.cfg["fatigue_exponent"])
        if self._day_state == "headache":
            mult *= 1.8
        return min(self.cfg["fatigue_cap"], mult)

    def _clock_hours(self) -> float:
        """Monotone hours since the wearer was created, for decay only."""
        return self._day * 24.0 + self._minutes / 60.0

    def _cost(self) -> float:
        """Total price of interrupting me right now, as a multiple of the bar."""
        raw = (self._day_cost() * self._fatigue_cost() * (1.0 + 0.25 * self._fragments))
        return min(self.cfg["cost_ceiling"], raw)

    def _redundancy(self, topic: str) -> float:
        """
        Diminishing returns inside one topic.

        The first item about a project is news, the tenth is a running
        commentary. Expanding is a strong signal that I care -- and the third
        expansion is worth far less than the first, because by then I already
        have the shape of it.
        """
        h = self._topic_hist.get(topic)
        if not h:
            return 1.0
        gap_h = max(0.0, self._clock_hours() - h["last"])
        mult = (self.cfg["repeat_base"] ** max(0, h["seen"] - 1))
        mult *= (self.cfg["expand_satiation"] ** max(0, h["expands"] - 1))
        # A day away makes a boring subject interesting again.
        mult = min(1.0, mult + self.cfg["recency_recovery"] * min(1.0, gap_h / 24.0))
        return max(0.10, mult)

    def _already_knew(self, item: dict) -> bool:
        """
        Did the laptop already tell me this?

        Three ways this fails in the field, all modelled: I forget what I was
        told four hours ago, so a redundant push gets a clean ``worth=1``
        (recognition_miss_rate); the system only knows what *it* surfaced, not
        what my laptop or a colleague said; and I re-notify known things to
        myself constantly, so checking a calendar is not an interruption.

        The first is the dangerous one: it makes ``already_know`` labels
        optimistic, so a stop criterion reading ``redundant < earned`` can look
        healthy while the product is failing.
        """
        explicit = item.get("already_knew")
        if isinstance(explicit, bool):
            return explicit
        topics = self._topics(item)
        if not topics:
            return False
        told = sum(1 for t in topics if self._known_topics.get(t, 0) > 0)
        if told == 0:
            return False
        if told == len(topics) and item.get("laptop_surfaced"):
            return True
        # Told before, but I may not connect this new item to it. Being human
        # about this is the adversarial part.
        return self.rng.random() >= self.cfg["recognition_miss_rate"]

    # --- the four public reactions ---
    def should_interrupt(self, item: dict, display_text: str) -> bool:
        """
        Break through, or stay silent?

        Rules of engagement, deliberately: utility over *cost*, not utility over
        a fixed threshold, so a cheap interruption needs less to be worth it
        than an expensive one; already-known information subtracts rather than
        vetoes, because I will still glance at it, I just will not be dragged
        out of flow; and unlegible text subtracts, because a fact I cannot read
        in one look is not information, it is a reason to look again and be
        annoyed.
        """
        self._sync(item)
        self._n_seen += 1

        # Observing the event updates what I know and what I have seen of it. This is
        # the only channel by which history enters the model.
        topics = self._topics(item)
        for t in topics:
            h = self._topic_hist.setdefault(t, {"seen": 0, "expands": 0, "last": 0.0})
            h["seen"] += 1
            h["last"] = self._clock_hours()
        if item.get("laptop_surfaced"):
            for t in topics:
                self._known_topics[t] = self._known_topics.get(t, 0) + 1

        legible = self._legible(display_text)
        utility = self._utility(item, legible)
        self._lens[display_text] = {
            "legible": legible, "truncated": self._truncated(display_text),
            "value": utility,
        }

        if self._abandoned_day is not None or self._day_state == "abandoned":
            return False

        score = utility / max(1e-6, self._cost())

        # Curiosity: a small rate of engaging with something I do not care
        # about. Real people are not a threshold function, and a system
        # claiming 0.99 precision has to survive these.
        if utility < self.cfg["threshold"] and self.rng.random() < self.cfg["curiosity_rate"]:
            score += self.cfg["threshold"] * 0.9

        if score < self.cfg["threshold"]:
            return False

        # ---- it fires. The cost lands immediately and unconditionally. ----
        self._n_interrupts += 1
        self._n_shown += 1
        self._fragments += 0.5
        # Deferred verdict: whether this counted as *bad* is only known once I
        # respond, but I already lost the attention. This is what makes pushing
        # into a void expensive -- the harness may never call responds().
        item["_pending_interrupt"] = True
        self._pending.append({"t": self._clock_hours(), "item": item})
        return True

    def _utility(self, item: dict, legible: bool) -> float:
        """Latent value of this item to me, on a ~0..2 scale."""
        topics = self._topics(item)
        relevance = self._relevance(item)
        urgency = self._urgency(item)

        redundancy = 1.0
        if topics:
            redundancy = min(self._redundancy(t) for t in topics)

        # Stance: I engage more with things that agree with what I already
        # believe, and discount things that argue with me. Slightly irrational,
        # extremely real, and invisible to a relevance score.
        stance = item.get("stance")
        if stance == "confirm":
            relevance *= 1.15
        elif stance == "challenge":
            relevance *= 0.85

        u = relevance * 1.0 + urgency * 0.55
        u *= redundancy
        u += 0.35 if legible else -0.15

        if self._already_knew(item):
            # Subtraction, not veto: I glance, I do not get dragged out of it.
            u -= self.cfg["known_penalty"]
            repeats = max((self._known_topics.get(t, 0) for t in topics), default=0)
            if repeats > 1:
                u -= self.cfg["redundancy_compound"] * (repeats - 1)

        u += self.rng.gauss(0.0, self.cfg["noise_sigma"])
        if self._day_state == "headache":
            u -= 0.25
        return u

    def glance_worth(self, item: dict, display_text: str) -> int:
        """
        0 ignorable, 1 worth a glance, 2 read this now.

        Glancing is cheap, so this asks a different question from the interrupt
        gate -- is there anything here for me at all -- and mostly ignores last
        hour's badness. That separation is why the queue can be generous while
        the push path stays brutal.
        """
        self._sync(item)
        legible = self._legible(display_text)
        u = self._utility(item, legible)
        # Residual cost only: a day's tiredness, not last hour's badness.
        u /= max(0.8, self._day_cost())
        if self._abandoned_day is not None:
            return 0

        if u >= self.cfg["threshold"] + 0.85:
            return 2
        if u >= self.cfg["threshold"] - 0.35:
            return 1
        return 0

    def responds(self, item: dict) -> dict:
        """
        My verdict on an item that was actually put in front of me.

        Two fields, deliberately orthogonal: ``worth`` (should this have taken
        my attention?) and ``already_knew`` (did I have this already?). The
        design's stop criterion depends on them staying separable, so they do:
        plenty of items are urgent, true, and completely redundant.
        """
        self._sync(item)
        known = self._already_knew(item)
        topics = self._topics(item)
        legible = self._lens_lookup(item)

        u = self._utility(item, legible)
        worth = 1 if u >= self.cfg["threshold"] - 0.15 else 0

        # The interrupt itself cost attention no matter how good it was.
        self._fragments = max(0.0, self._fragments - 0.15)
        self._settle(item, worth, known)
        if worth and known:
            # Worth knowing, zero reason to interrupt: the redundant case.
            self._n_redundant += 1
        return {"worth": int(worth), "already_knew": int(known)}

    def _lens_lookup(self, item: dict) -> bool:
        """Legibility of whatever was actually shown for this item."""
        for text in (item.get("display_text"), item.get("summary"), item.get("line"), ""):
            if text and text in self._lens:
                return bool(self._lens[text]["legible"])
        return self._legible(item.get("display_text") or item.get("summary") or "")

    def _settle(self, item: dict, worth: int, known: int) -> None:
        """Where trust and fatigue move. This is the adversarial core."""
        self._n_shown += 1
        self._pending = [p for p in self._pending if p["item"] is not item]
        if not worth or known:
            # Two kinds of waste, and they are not the same. A wrong item is
            # noise; a right-but-known item is worse in a way the design's
            # false_alarm count cannot see, because nothing false was said.
            self._register_bad(known=bool(known))
        else:
            self._trust = min(1.0, self._trust + self.cfg["trust_gain_good"])

    def _register_bad(self, known: bool, extra_trust: float = 0.0) -> None:
        """One unit of wasted attention. Non-linear in effect, unbounded in
        consequence: enough of these and the experiment ends early."""
        self._n_bad += 1
        self._bad.append(self._clock_hours())
        # Trust loss is per-bad and permanent-ish; there is no 24-hour
        # auto-suspend that brings me back. This is the design assumption I
        # violate most directly.
        loss = self.cfg["trust_decay_bad"] + extra_trust
        self._trust -= loss * (1.35 if known else 1.0)
        if self._trust <= 0.0 and self._abandoned_day is None:
            self._abandoned_day = self._day
            self._day_state = "abandoned"

    def gaze(self, display_text: str) -> dict:
        """
        Did I look up, for how long, and did I come back?

        Re-examination is driven by *legibility*, not by a coin flip: illegible
        text means I cannot tell what it is, so I do not come back (and a forced
        second look ends in irritation); legible text longer than one look means
        I come back to finish it; legible and short means one look, done.
        Truncation mid-fact is its own strong pull.
        """
        self._headups += 1
        text = str(display_text or "")
        state = self._lens.get(text)
        if state is None:
            # Something reached the lens that I was never asked about.
            state = {"legible": self._legible(text),
                     "truncated": self._truncated(text),
                     "value": 0.5}
            self._lens[text] = state

        if self._abandoned_day is not None:
            return {"examined": False, "re_examined": False, "dwell_ms": 0}

        # Do I even register the peripheral cue? Habituation means a cue that
        # has been noise five times stops being noticed -- so head-up can be a
        # *lower* bound on examination, not just an upper one.
        notice = max(
            self.cfg["notice_floor"],
            self.cfg["notice_base"] * math.pow(
                max(0.0, 1.0 - self.cfg["notice_habituation"] * self._headups), 1.3
            ),
        )
        notice *= 0.75 if self._day_state == "headache" else 1.0
        examined = self.rng.random() < notice
        if not examined:
            return {"examined": False, "re_examined": False, "dwell_ms": 0}

        self._n_examined += 1
        n = len(text)
        per_char = MS_PER_CHAR_EASY if state["legible"] else MS_PER_CHAR_HARD
        value = float(state["value"])
        dwell = n * per_char * (0.65 + 0.5 * min(1.5, value))
        # Interest holds the eye: an interesting card is read twice as slowly
        # the first time because you are actually thinking about it.
        if value >= self.cfg["threshold"]:
            dwell *= 1.25

        re_examined = False
        if state["legible"]:
            pull = 0.0
            if n > HEAD_CHARS:               # more than one look's worth
                pull += self.cfg["re_examine_gain"]
            if state["truncated"]:           # cut off mid-fact
                pull += 0.35
            if value >= self.cfg["threshold"] + 0.6:
                pull += 0.25
            pull -= 0.25 * self._fatigue_cost()   # tired: less second-guessing
            re_examined = self.rng.random() < max(0.0, min(0.85, pull))
        else:
            # Illegible: no return visit -- unless I was interrupted and still
            # do not know what it said, in which case I look again and leave
            # angrier than I arrived.
            if self._fragments > 0.8 and n > HEAD_CHARS:
                re_examined = self.rng.random() < 0.18
                if re_examined:
                    self._n_unreadable_reeads += 1
                    self._trust -= 0.04
                    if self._trust <= 0.0 and self._abandoned_day is None:
                        self._abandoned_day = self._day
                        self._day_state = "abandoned"

        if re_examined:
            self._n_reexamined += 1
            dwell += n * per_char * 0.45

        dwell_ms = int(max(350.0, min(DISPLAY_CAP_MS, dwell)))
        return {"examined": True, "re_examined": re_examined, "dwell_ms": dwell_ms}

    # --- reporting only; never read back into a decision ---
    def report(self) -> dict:
        """
        Hidden state, for the harness to print after the run.

        The only way hidden interest escapes, and a one-way valve: nothing in
        the decision path calls it, and it never touches the RNG, so printing
        cannot change the experiment.
        """
        ranked = sorted(self._weights.items(), key=lambda kv: -kv[1])
        counts = {k[3:]: v for k, v in vars(self).items() if k.startswith("_n_")}
        return {
            "seed": self.seed, "day": self._day, "state": self._day_state,
            "minute_of_day": round(self._minutes, 1),
            "hidden_weights": {k: round(v, 4) for k, v in ranked},
            "top_subject": ranked[0][0] if ranked else None,
            "regime_shift_day": self._regime_shift_day,
            "focus_shift_day": self._focus_shift_day,
            "drift": self._drift_log,
            "attention": {
                "cost_now": round(self._cost(), 3),
                "day_cost": round(self._day_cost(), 3),
                "fatigue_cost": round(self._fatigue_cost(), 3),
                "live_bad": len(self._bad), "trust": round(self._trust, 3),
                "fragments": round(self._fragments, 3),
            },
            "counts": counts | {"headups": self._headups},
            "abandoned_day": self._abandoned_day,
        }