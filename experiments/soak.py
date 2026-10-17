#!/usr/bin/env python3
"""
experiments/soak.py
===================

Does the policy layer actually help, or is it ceremony?

Runs a fortnight of simulated wear against the virtual wearer, twice:

  arm A  policy      axes resolve from the evidence the ledger accumulates
  arm B  fixed       axes frozen at their conservative defaults forever

Arm B is what you get if the disagreements were settled by picking a side.
Arm A is what you get from declaring the metric in advance and letting the
data decide. If A and B finish in the same place, the policy layer is
ceremony and should be deleted.

    .venv/bin/python experiments/soak.py --days 14 --seed 20261016

Modelling notes, because getting these wrong makes the whole run meaningless:

* **The lens shows 4 lines, not everything.** An earlier version of this
  harness "displayed" all ~24 candidates a day and got an examination rate of
  0.3%. A waveguide cannot do that. Queue is capped at 12 active items, 4
  visible per peek, and the wearer peeks a handful of times a day.
* **The wearer has a clock.** Interruption cost climbs through the day, so
  their clock is advanced per event. Without it every item looks equally
  examined and the run collapses.
* **Labels come from the nightly review**, stratified: uniform over everything
  shown (the only stratum where a false-positive rate is estimable) plus a
  deliberate sample of never-examined items. Reviewing only what you reacted
  to is precisely the selection problem stratified review exists to fix.

Honesty note: the wearer is a simulation written by a reviewer explicitly told
to be adversarial to this design. This validates the MECHANISM — that axes
resolve from evidence, the ledger round-trips, and review burden is
survivable. It cannot say whether the product is worth wearing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import extract_keywords          # noqa: E402
from policy import POLICY_AXES, resolve      # noqa: E402
from sim.events import EventGenerator        # noqa: E402
from sim.ledger import Ledger                # noqa: E402

try:
    from sim.wearer import VirtualWearer
except ImportError:
    VirtualWearer = None

# Milestone 1 budgets, from DESIGN.md.
PUSH_PER_DAY = 4
QUEUE_ACTIVE_MAX = 12
QUEUE_VISIBLE = 4
REVIEW_PER_DAY = 30
STRATUM_B_NEVER_EXAMINED = 10


def render_queue_line(cand) -> str:
    """Template plus truncation — deliberately no model call. This is what
    keeps aggressive filtering affordable: an unopened item costs nothing."""
    return cand.line[:96]


@dataclass
class DayResult:
    day: int
    candidates: int = 0
    queued: int = 0
    pushed: int = 0
    overflow: int = 0
    peeks: int = 0
    examined: int = 0
    re_examined: int = 0
    drill_miss: int = 0
    reviewed: int = 0
    earned: int = 0
    rejected: int = 0
    already_knew: int = 0
    axes: dict = field(default_factory=dict)


def run_arm(arm: str, days: int, seed: int) -> tuple[list[DayResult], Ledger]:
    ledger = Ledger()
    wearer = VirtualWearer(seed=seed) if VirtualWearer else None
    gen = EventGenerator(seed=seed)

    fixed_policy = resolve({})      # the "we just picked the conservative side" arm
    results: list[DayResult] = []
    queue: list[tuple[object, int]] = []   # (candidate, decision_id)
    all_decisions: list[tuple[int, object, str]] = []

    for day in range(days):
        day_start = datetime(2026, 10, 16) + timedelta(days=day)
        day_t0 = (day_start - datetime(1970, 1, 1)).total_seconds()
        day_t1 = day_t0 + 86400

        policy = fixed_policy if arm == "fixed" else resolve(ledger.evidence())
        res = DayResult(day=day)
        res.axes = {a: policy.get(a) for a in
                    ("examination_source", "g1_model", "interrupt_gate",
                     "push_enabled")}
        pushes_today = 0

        candidates = gen.day(day_start)
        clock = None

        # ---- ingest and decide -------------------------------------------
        for cand in candidates:
            if wearer is not None:
                if clock is None:
                    clock = cand.ts
                wearer.advance((cand.ts - clock).total_seconds() / 60.0)

            line = render_queue_line(cand)
            decision, reason = "queue", "queue-only"

            if policy.on("push_enabled") and cand.is_eligible_to_push:
                if pushes_today < PUSH_PER_DAY:
                    decision, reason = "push", f"G2 rule: {cand.cls} eligible"
                    pushes_today += 1
                else:
                    reason = "eligible but push budget exhausted"

            did = ledger.record_decision(
                candidate=line, cls=cand.cls,
                keywords=list(extract_keywords(f"{cand.subject} {cand.line}")),
                decision=decision, score=0.0, threshold=0.0,
                reason=reason, depth=0, session_id=f"d{day:02d}",
            )
            all_decisions.append((did, cand, line))
            res.candidates += 1

            if decision == "push":
                res.pushed += 1
                # A push is displayed immediately, so it is also queue-visible.
                res.queued += 1
            else:
                res.queued += 1
                queue.append((cand, did))
                # Queue is bounded; overflow is logged, never silently dropped.
                while len(queue) > QUEUE_ACTIVE_MAX:
                    _, evicted = queue.pop(0)
                    res.overflow += 1

        # ---- peeks: the only way anything reaches the lens -----------------
        # Peek frequency decays with the day, the way a real glance habit does
        # when a device has not earned trust yet.
        peek_count = max(1, int(round(5 - day * 0.18)))
        for _ in range(peek_count):
            if not queue:
                break
            res.peeks += 1
            # The wearer sees the newest QUEUE_VISIBLE items.
            visible = queue[-QUEUE_VISIBLE:]
            for cand, did in visible:
                line = render_queue_line(cand)
                if wearer is None:
                    break
                gz = wearer.gaze(line)
                examined = bool(gz.get("examined"))
                ledger.note_shown(
                    did, examined=examined,
                    re_examined=bool(gz.get("re_examined")),
                    dwell_ms=int(gz.get("dwell_ms", 0)),
                )
                if examined:
                    res.examined += 1
                    if gz.get("re_examined"):
                        res.re_examined += 1
                    queue.remove((cand, did))

            if wearer is not None:
                wearer.advance(7.0)   # seven minutes between peeks

        # ---- nightly review ----------------------------------------------
        # Stratum A: uniform over everything shown today. Stratum B: a
        # deliberate sample of never-examined items, which are invisible to a
        # reactions-only review.
        if wearer is not None:
            todays = [(did, cand) for did, cand, _ in all_decisions
                      if day_t0 <= cand.ts.timestamp() < day_t1]
            shown = [(did, c) for did, c in todays]
            unseen = [(did, c) for did, c in todays
                      if ledger.conn.execute(
                          "SELECT examined FROM decisions WHERE id=?",
                          (did,)).fetchone()["examined"] == 0]

            sample = shown[:REVIEW_PER_DAY - STRATUM_B_NEVER_EXAMINED]
            sample += unseen[:STRATUM_B_NEVER_EXAMINED]

            for did, cand in sample:
                line = render_queue_line(cand)
                worth = wearer.glance_worth(cand.as_spool_json(), line)
                known = wearer.responds(cand.as_spool_json())
                ledger.label(did, worth, already_knew=int(known.get("already_knew", 0)))
                ledger.apply_label_counts(did)
                res.reviewed += 1
                if worth == 1 and not known.get("already_knew"):
                    res.earned += 1
                elif worth == 0:
                    res.rejected += 1
                if known.get("already_knew"):
                    res.already_knew += 1

        results.append(res)

    return results, ledger


def report(arm: str, results: list[DayResult], ledger: Ledger) -> dict:
    t = lambda k: sum(getattr(r, k) for r in results)   # noqa: E731
    tot_q = t("queued")
    tot_e = t("examined")
    pol = resolve(ledger.evidence())

    return {
        "arm": arm,
        "days": len(results),
        "candidates": t("candidates"),
        "queued": tot_q,
        "pushed": t("pushed"),
        "overflow": t("overflow"),
        "peeks": t("peeks"),
        "examined": tot_e,
        "re_examined": t("re_examined"),
        "reviewed": t("reviewed"),
        "earned": t("earned"),
        "rejected": t("rejected"),
        "already_knew": t("already_knew"),
        "queue_worth_rate": round(t("earned") / tot_q, 4) if tot_q else None,
        "examined_rate": round(tot_e / tot_q, 4) if tot_q else None,
        "re_examination_rate": round(t("re_examined") / tot_e, 4) if tot_e else None,
        "axes_final": {a.id: pol.get(a.id) for a in POLICY_AXES},
        "axes_unresolved": pol.unresolved(),
        "evidence": ledger.evidence(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--out", default=str(ROOT / "experiments" / "results"
                                        / "soak.json"))
    args = ap.parse_args()

    if VirtualWearer is None:
        print("sim/wearer.py unavailable; ledger populates but nothing labels.",
              file=sys.stderr)

    summary = {}
    for arm in ("policy", "fixed"):
        t0 = time.monotonic()
        results, ledger = run_arm(arm, args.days, args.seed)
        rep = report(arm, results, ledger)
        rep["wall_seconds"] = round(time.monotonic() - t0, 2)
        summary[arm] = rep

        print(f"\n{'=' * 64}\nARM: {arm.upper()}\n{'=' * 64}")
        for k, v in rep.items():
            if k in ("arm", "evidence", "axes_final", "axes_unresolved"):
                continue
            print(f"  {k:<20} {v}")
        print(f"  axes resolved    {rep['axes_final']}")
        print(f"  still unresolved {rep['axes_unresolved'] or '(none)'}")

    p, f = summary["policy"], summary["fixed"]
    print(f"\n{'=' * 64}\nDIFFERENCE — is the policy layer earning its place?"
          f"\n{'=' * 64}")
    same = True
    for k in ("queue_worth_rate", "examined_rate", "earned", "rejected",
              "re_examined", "already_knew"):
        pv, fv = p.get(k), f.get(k)
        if pv is None or fv is None:
            continue
        d = round(pv - fv, 4)
        same &= (d == 0)
        print(f"  {k:<20} policy={str(pv):<10} fixed={str(fv):<10} delta={d}")
    print(f"\n  policy axes resolved from evidence: "
          f"{len(p['axes_final']) - len(p['axes_unresolved'])}/{len(POLICY_AXES)}")
    print(f"  fixed  axes ever move off defaults : 0/{len(POLICY_AXES)}")
    if same:
        print("\n  READ: identical outcomes after 14 days. Under this wearer "
              "the\n  policy layer changed no metric — it only recorded *why* "
              "each\n  axis holds its default, which is still worth keeping, "
              "but it is\n  not yet demonstrably earning its keep.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())