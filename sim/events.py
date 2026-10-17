"""
sim/events.py
=============

Synthetic candidate generator — the "agent spool" from DESIGN.md.

The whole relay system is downstream of events arriving, and every reviewer
agreed that nothing can be calibrated without them. This module produces a
realistic day of them so the pipeline, the ledger and the review loop can be
exercised end to end with no glasses and no calendar.

Candidate classes, per DESIGN.md G2:

    meeting       accepted meeting within the horizon   -> eligible to push
    blocked_job   own agent, dispatched, >120s         -> eligible to push
    device        battery transition below 10%         -> eligible to push
    build         an agent finished something          -> queue only
    mention       someone replied / mentioned you      -> queue only
    dependency    a package or service changed         -> queue only
    digest        a scheduled roll-up                   -> queue only
    social        likes, views, reactions              -> queue only
    spam          marketing, newsletters               -> noise

Time-of-day structure matters as much as the classes: meetings cluster in
working hours, social noise clusters mid-evening, and the first hour of the
day is when a wearer's attention is cheapest.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

CLASSES = (
    "meeting", "blocked_job", "device",
    "build", "mention", "dependency", "digest",
    "social", "spam",
)

# Events that carry a real deadline the wearer could act on.
DEADLINE_CLASSES = frozenset({"meeting", "blocked_job", "device"})


@dataclass
class Candidate:
    """One thing that wants to reach the lens."""

    cid: str
    source: str
    cls: str
    line: str
    ts: datetime
    subject: str
    keywords: tuple[str, ...] = ()
    blocked: bool = False
    accepted: bool = True
    elapsed_s: int = 0
    minutes_until: float | None = None
    meta: dict = field(default_factory=dict)

    def as_spool_json(self) -> dict:
        """The on-disk form: ~/.g1/outbox/*.json."""
        return {
            "source": self.source,
            "class": self.cls,
            "line": self.line,
            "ts": self.ts.isoformat(),
            "subject": self.subject,
            "keywords": list(self.keywords),
            "blocked": self.blocked,
            "accepted": self.accepted,
            "elapsed_s": self.elapsed_s,
            "minutes_until": self.minutes_until,
        }

    @property
    def is_eligible_to_push(self) -> bool:
        """DESIGN.md G2, the deterministic eligibility rules."""
        if self.cls == "meeting":
            return self.accepted and self.minutes_until is not None
        if self.cls == "blocked_job":
            return self.blocked and self.elapsed_s > 120
        if self.cls == "device":
            return self.meta.get("battery_below_10", False)
        return False


# bucket -> {display subject: keywords it implies}
SUBJECTS: dict[str, dict[str, tuple[str, ...]]] = {
    "glasses": {
        "g1-protocol": ("g1", "glasses", "waveguide", "touchbar", "firmware"),
        "glasses-mtu": ("mtu", "ble", "notification"),
    },
    "infra": {
        "deploy": ("deploy", "server", "latency"),
        "outage": ("outage", "monitoring", "server"),
    },
    "study": {
        "attention-design": ("attention", "notification", "protocol"),
    },
}

PEOPLE = ["Alex", "Caleb", "Sam", "Rin", "Jo", "Priya", "Marek"]


class EventGenerator:
    """
    Produces a plausible day of candidates.

    Subject mix is *weighted* rather than uniform so the stream resembles a
    real person: mostly noise, a few meaningful things, occasional genuine
    urgency, with class frequencies shifting by hour.
    """

    def __init__(self, seed: int = 20261016) -> None:
        self.rng = random.Random(seed)
        self.counter = 0

    def _subject(self, bucket: str) -> tuple[str, tuple[str, ...]]:
        name, kws = self.rng.choice(list(SUBJECTS[bucket].items()))
        return name, kws

    def _hour_weights(self) -> dict[str, float]:
        """Class mix by hour of day."""
        base = {
            "meeting": 0.9, "blocked_job": 1.0, "device": 0.2,
            "build": 1.1, "mention": 1.0, "dependency": 0.8,
            "digest": 0.9, "social": 0.6, "spam": 1.4,
        }
        if self.rng.random() < 0.25:
            # Evening: social and marketing rise, engineering falls away.
            base = {**base, "social": 2.2, "spam": 2.6, "build": 0.4,
                    "dependency": 0.3, "mention": 1.4}
        return base

    def _make(self, now: datetime, cls: str) -> Candidate:
        self.counter += 1
        cid = f"c{self.counter:05d}"

        if cls == "meeting":
            subject, kws = "meeting", ("meeting",)
            mins = self.rng.choice([4, 9, 12, 18, 25, 40, 55, 90, 240])
            who = self.rng.choice(PEOPLE)
            hour = (now + timedelta(minutes=mins)).strftime("%H:%M")
            return Candidate(
                cid, "calendar", cls, f"{who} at {hour} in {mins} min",
                now, subject, kws, accepted=self.rng.random() > 0.25,
                minutes_until=float(mins),
            )

        if cls == "blocked_job":
            subject, kws = self._subject("glasses")
            elapsed = self.rng.choice([35, 90, 140, 260, 400, 900])
            return Candidate(
                cid, "agent", cls,
                f"{subject} build still running after {elapsed // 60}m",
                now, subject, kws,
                blocked=self.rng.random() > 0.45, elapsed_s=elapsed,
            )

        if cls == "device":
            pct = self.rng.choice([4, 7, 9, 11, 14])
            return Candidate(
                cid, "glasses", cls, f"Glasses battery {pct}%",
                now, "battery", ("battery", "glasses"),
                meta={"battery_pct": pct, "battery_below_10": pct < 10},
            )

        if cls == "build":
            subject, kws = self._subject("glasses")
            n = self.rng.randint(3, 60)
            ok = self.rng.random() > 0.35
            state = f"{n} tests passed" if ok else f"{self.rng.randint(1,4)} tests failed"
            return Candidate(
                cid, "agent", cls, f"{subject}: {state}",
                now, subject, kws, elapsed_s=self.rng.randint(10, 900),
            )

        if cls == "mention":
            who = self.rng.choice(PEOPLE)
            age_h = self.rng.choice([0, 2, 6, 26, 70])
            when = "just now" if age_h == 0 else f"{age_h}h ago"
            subject, kws = self._subject("infra")
            return Candidate(
                cid, "chat", cls, f"{who} mentioned you about {subject} ({when})",
                now, subject, kws, elapsed_s=age_h * 3600,
            )

        if cls == "dependency":
            pkg = self.rng.choice(["tev1", "winnow", "bleak", "openai", "fastapi"])
            impact = self.rng.choice(["security fix", "major bump", "patch"])
            return Candidate(
                cid, "deps", cls, f"{pkg} has an update ({impact})",
                now, pkg, (pkg, "dependency"),
            )

        if cls == "digest":
            n = self.rng.randint(3, 40)
            return Candidate(
                cid, "digest", cls, f"{n} unread in your digest",
                now, "digest", ("digest", "unread"),
            )

        if cls == "social":
            kind = self.rng.choice(
                ["viewed your profile", "liked your post", "reacted to your comment"])
            return Candidate(
                cid, "social", cls, f"{self.rng.randint(3,9)} people {kind}",
                now, "social", ("social",),
            )

        # spam
        brand = self.rng.choice(["Weekly Digest", "Promo", "Newsletter",
                                 "Deal Alert", "Recap"])
        return Candidate(
            cid, "marketing", cls,
            f"{brand}: {self.rng.randint(12, 60)} stories",
            now, "marketing", ("marketing", "newsletter"),
        )

    def day(self, day: datetime, count: int | None = None) -> list[Candidate]:
        """One day of candidates, interleaved through working hours."""
        n = count if count is not None else self.rng.randint(14, 34)
        weights = self._hour_weights()
        classes = list(weights)
        probs = [weights[c] for c in classes]

        out: list[Candidate] = []
        start = day.replace(hour=8, minute=0, second=0, microsecond=0)
        for i in range(n):
            when = start + timedelta(
                minutes=self.rng.uniform(0, 13 * 60), seconds=self.rng.random()
            )
            cls = self.rng.choices(classes, weights=probs, k=1)[0]
            out.append(self._make(when, cls))

        # Meetings are scheduled against real start times rather than arrival.
        out.sort(key=lambda c: c.ts)
        return out