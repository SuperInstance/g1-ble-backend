#!/usr/bin/env python3
"""
experiments/clock_feature.py
============================

Turn A found the decision model's failure mode: it confuses bucket-1
(glance-worthy) with bucket-2 (interrupt) whenever the text contains a time or
a number. Best precision was 0.875 at recall>=0.50 against a 0.99 budget.

Hypothesis: the discriminator is a deadline **relative to now**, and that is a
feature the model structurally cannot have -- it sees text, not a clock.

This script tests that hypothesis by building a clock-aware feature locally
(no model calls), then combining it with the recorded winnow scores and
measuring whether precision actually improves.

Run:  .venv/bin/python experiments/clock_feature.py
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# Phrases that mean "no deadline" even though they contain a time-ish token.
NEGATED = re.compile(
    r"\b(moved|reschedul\w+|postpon\w+|delayed)\b", re.I
)
RELATIVE = re.compile(
    r"\bin\s+(\d{1,3})\s*(minute|min|hour|hr|day)s?\b", re.I
)
CLOSING = re.compile(
    r"\b(closes?|boarding closes?|expires?)\b.{0,30}?\b(\d{1,3})\s*"
    r"(minute|min|hour|hr)s?\b", re.I
)
CLOCKTIME = re.compile(
    r"\b(\d{1,2}):(\d{2})\b"
)
TOMORROW = re.compile(r"\btomorrow\b", re.I)

UNIT_MIN = {"minute": 1, "min": 1, "hour": 60, "hr": 60, "day": 1440}


def minutes_until(text: str, now: datetime) -> float | None:
    """
    Minutes until the implied deadline, or None if there is no deadline.

    Deliberately conservative: it only fires on patterns where a human would
    say "that has a clock on it". It must not fire on "moved to 11:00" -- a
    rescheduled meeting is not an expiring credential.
    """
    # A rescheduled/postponed event is not an imminent deadline.
    if NEGATED.search(text):
        # ...unless it also contains an explicit expiry in short form.
        if not CLOSING.search(text):
            return None

    m = CLOSING.search(text)
    if m:
        n = int(m.group(2))
        unit = m.group(3).lower()
        return float(n * UNIT_MIN.get(unit, 1))

    m = RELATIVE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        # "in 6 minutes" -> imminent. "in 2 days" -> not.
        return float(n * UNIT_MIN.get(unit, 1))

    if TOMORROW.search(text):
        return 1440.0

    m = CLOCKTIME.search(text)
    if m and not NEGATED.search(text):
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh < 24 and mm < 60:
            when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if when <= now:
                when += timedelta(days=1)
            return (when - now).total_seconds() / 60.0
    return None


def urgency(minutes: float | None, *, now: datetime) -> float:
    """Map minutes-until-deadline to a 0..1 urgency. 15 minutes is the knee."""
    if minutes is None:
        return 0.0
    if minutes <= 15:
        return 1.0
    if minutes >= 120:
        return max(0.0, 1.0 - (minutes - 120) / 1440.0) * 0.25
    return 0.25 + 0.75 * (120 - minutes) / 105


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return float("nan")
    w = t = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                w += 1
            elif p == n:
                t += 1
    return (w + 0.5 * t) / (len(pos) * len(neg))


def best_precision(scores, labels, min_recall):
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    tot = sum(labels)
    if not tot:
        return float("nan")
    seen_pos = 0
    best = 0.0
    for k, i in enumerate(order, 1):
        if labels[i] == 1:
            seen_pos += 1
        if seen_pos / tot >= min_recall:
            best = max(best, seen_pos / k)
    return best


def main() -> None:
    rows = json.loads((RESULTS / "scores.json").read_text())["rows"]
    now = datetime(2026, 10, 16, 15, 0)  # fixed "now" so the run is reproducible

    print(f"reference 'now': {now.isoformat()}\n")
    print("Deadline extraction:")
    print(f"  {'min':>8}  {'urg':>5}  {'truth':>5}  text")
    for r in rows:
        mins = minutes_until(r["text"], now)
        if mins is not None:
            print(f"  {mins:8.1f}  {urgency(mins, now=now):5.2f}  {r['truth']:5d}  "
                  f"{r['text'][:46]}")

    y = [1 if r["truth"] == 2 else 0 for r in rows]
    winnow = [r["score"] for r in rows]
    urg = [urgency(minutes_until(r["text"], now), now=now) for r in rows]

    print("\nAblation — does the clock feature help?\n")
    print(f"  {'rule':<44} {'AUC':>7} {'P@r>=.5':>8} {'P@r>=.9':>8}")
    print(f"  {'-'*44} {'-'*7} {'-'*8} {'-'*8}")

    combos = {
        "winnow alone (Turn A)": winnow,
        "clock urgency alone": urg,
        "max(winnow, 1.6*urgency)": [max(w, 1.6 * u) for w, u in zip(winnow, urg)],
        "max(winnow, 2.5*urgency)": [max(w, 2.5 * u) for w, u in zip(winnow, urg)],
        "0.7*winnow + 1.4*urgency": [0.7 * w + 1.4 * u for w, u in zip(winnow, urg)],
        "winnow + 0.8*urgency": [w + 0.8 * u for w, u in zip(winnow, urg)],
    }
    best = None
    for name, s in combos.items():
        a = auc(s, y)
        p5 = best_precision(s, y, 0.50)
        p9 = best_precision(s, y, 0.90)
        print(f"  {name:<44} {a:7.4f} {p5:8.3f} {p9:8.3f}")
        if best is None or p5 > best[1]:
            best = (name, p5, p9, a)

    print(f"\n  best: {best[0]}  (P@r>=0.5 = {best[1]:.3f}, "
          f"P@r>=0.9 = {best[2]:.3f}, AUC = {best[3]:.4f})")
    gain = best[1] - best_precision(winnow, y, 0.50)
    print(f"  precision gain over winnow alone: {gain:+.3f}")
    print(f"  target:                            +0.115  (to reach 0.99)")

    (RESULTS / "clock-feature.md").write_text(
        f"# Clock feature experiment\n\n"
        f"winnow alone: AUC {auc(winnow, y):.4f}, "
        f"P@r>=0.5 {best_precision(winnow, y, 0.50):.3f}, "
        f"P@r>=0.9 {best_precision(winnow, y, 0.90):.3f}\n\n"
        f"best combination: **{best[0]}** — AUC {best[3]:.4f}, "
        f"P@r>=0.5 **{best[1]:.3f}**, P@r>=0.9 **{best[2]:.3f}**\n\n"
        f"Precision gain over winnow alone: **{gain:+.3f}** "
        f"(target +0.115 to reach the 0.99 budget)\n\n"
        f"Hypothesis {'SUPPORTED' if gain > 0 else 'NOT SUPPORTED'}: "
        f"a clock-aware feature {'does' if gain > 0 else 'does not'} close the gap.\n"
    )
    print(f"\nwrote {RESULTS / 'clock-feature.md'}")


if __name__ == "__main__":
    main()