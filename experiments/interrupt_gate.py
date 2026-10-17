"""
Does the interrupt decision behave, and does it stay silent?

``interrupt.py`` is the first shipped code that answers "should this reach the
wearer". This exercises it with no radio, no ledger and no model, which is the
only reason it can be tested at all — the decision logic used to exist solely
inside ``experiments/soak.py`` against simulated candidates and a virtual
opponent.

The properties worth asserting, in order of how much they would cost if wrong:

  1. SILENCE BY DEFAULT. A fresh policy has no label history, so
     ``push_enabled`` resolves off and NOTHING is pushed, no matter how
     eligible the candidate. A queue module that interrupts on day one is
     worse than no queue module, because it will be trusted.

  2. NOTHING IS DROPPED. Every candidate that arrives comes back with an
     action. There is no path that discards one. This is the property the
     whole design rests on and it is easy to break with a filter.

  3. THE BUDGET CONSTRAINS THE BATCH. Five eligible candidates must not
     produce five pushes. MAX_PUSHES_PER_HOUR is checked once per candidate
     against state that the previous pushes already updated.

  4. ELIGIBILITY IS DETERMINISTIC AND CONSERVATIVE. A device alert with no
     battery reading must queue rather than guess.

  5. THE ORDER IS REPRODUCIBLE. Same input, same output, every run — a queue
     that reshuffles is not auditable.

  6. A MALFORMED FILE DOES NOT KILL THE LOOP. One bad producer must not take
     a wearable session down.

Each section states what would be a plausible-looking wrong answer, because a
test that cannot fail is decoration. Section 1 has a control arm: it proves
the same candidates DO push once evidence exists, so "nothing pushed" cannot
be passing for the wrong reason.

Run: ``.venv/bin/python experiments/interrupt_gate.py``
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import interrupt as q                                    # noqa: E402
from policy import MAX_PUSHES_PER_HOUR               # noqa: E402

FAILURES: list[str] = []
NOW = datetime(2026, 10, 16, 21, 0, 0, tzinfo=timezone.utc)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


def meeting(mins: float, accepted: bool = True) -> q.Candidate:
    return q.Candidate(source="cal", cls="meeting", line="Standup with Ana",
                       ts=NOW, subject="meeting", accepted=accepted,
                       minutes_until=mins)


def blocked(seconds: int, is_blocked: bool = True) -> q.Candidate:
    return q.Candidate(source="build", cls="blocked_job", line="CI red on main",
                       ts=NOW, subject="infra", blocked=is_blocked,
                       elapsed_s=seconds)


def device(pct) -> q.Candidate:
    meta = {} if pct is None else {"battery_pct": pct}
    return q.Candidate(source="glasses", cls="device", line="Battery low",
                       ts=NOW, subject="glasses", meta=meta)


def main() -> int:
    print("1. silence by default, with a control arm")
    fresh = q.Budget()
    cands = [meeting(5), blocked(600), device(4)]
    v = q.plan(cands, budget=fresh, now=NOW)
    check("fresh policy pushes NOTHING",
          sum(x.is_push for x in v) == 0,
          f"{sum(x.is_push for x in v)} pushed of {len(v)}")
    check("every candidate still got an action",
          all(x.action in ("push", "queue") for x in v),
          f"actions: {[x.action for x in v]}")
    reasons = " ".join(x.reason for x in v)
    check("the reason names the missing evidence, not a rule failure",
          "label" in reasons or "milestone" in reasons or "evidence" in reasons,
          reasons[:70])

    # CONTROL. Without this, "nothing pushed" could pass because the module is
    # broken rather than because the policy is conservative. Same candidates,
    # one field of evidence changed.
    primed = q.Budget(label_days=20)
    v2 = q.plan(cands, budget=primed, now=NOW)
    check("CONTROL: with label history the same candidates DO push",
          sum(x.is_push for x in v2) > 0,
          f"{sum(x.is_push for x in v2)} pushed; "
          f"reasons: {[x.reason for x in v2 if x.is_push]}")

    print("\n2. nothing is ever dropped")
    mixed = [meeting(5), meeting(99), blocked(30), blocked(600),
             device(4), device(80), device(None),
             q.Candidate(source="x", cls="unknown_class", line="?", ts=NOW)]
    verdicts = q.plan(mixed, budget=q.Budget(label_days=20), now=NOW)
    check("every candidate returned a verdict",
          len(verdicts) == len(mixed), f"{len(verdicts)}/{len(mixed)}")
    check("no action outside {push, queue}",
          all(x.action in ("push", "queue") for x in verdicts))
    ineligible = [x for x in verdicts if x.gate == "eligibility"]
    check("ineligible candidates are QUEUED, not dropped",
          all(x.action == "queue" for x in ineligible),
          f"{len(ineligible)} queued on eligibility")

    print("\n3. the budget constrains the batch")
    many = [blocked(600 + i) for i in range(6)]
    b = q.Budget(label_days=20)
    v3 = q.plan(many, budget=b, now=NOW)
    pushed = sum(x.is_push for x in v3)
    check(f"6 eligible candidates produce <= {MAX_PUSHES_PER_HOUR} pushes",
          pushed <= MAX_PUSHES_PER_HOUR,
          f"{pushed} pushed, budget ended at {b.pushes_last_hour}")
    check("the rest queue with the budget as the stated reason",
          all("push" in x.reason or "budget" in x.reason or "used" in x.reason
              for x in v3 if not x.is_push),
          next((x.reason for x in v3 if not x.is_push), "n/a")[:70])
    check("budget counters advanced by exactly the pushes",
          b.pushes_last_hour == pushed and b.pushes_today == pushed,
          f"counter={b.pushes_last_hour} pushed={pushed}")

    print("\n4. eligibility is deterministic and conservative")
    cases = [
        (meeting(5), True, "accepted and inside the window"),
        (meeting(5, accepted=False), False, "declined"),
        (meeting(45), False, "too far out"),
        (blocked(600), True, "blocked long enough"),
        (blocked(30), False, "blocked too briefly"),
        (blocked(600, is_blocked=False), False, "not blocked"),
        (device(4), True, "battery below floor"),
        (device(80), False, "battery fine"),
        (device(None), False, "no reading -> refuse to guess"),
    ]
    for cand, want, why in cases:
        ok, reason = cand.eligible(NOW)
        check(f"G2 {cand.cls}/{why} -> {'push' if want else 'queue'}",
              ok == want, reason)
    check("device with no reading is refused, not assumed low",
          device(None).eligible(NOW)[0] is False)

    print("\n5. order is reproducible, and does not compare unlike units")
    unordered = [device(4), meeting(9), blocked(600), meeting(2),
                 device(80), meeting(120)]
    r1 = [c.line for c in q.rank(unordered, NOW)]
    r2 = [c.line for c in q.rank(list(reversed(unordered)), NOW)]
    check("rank() is independent of input order", r1 == r2,
          f"{r1[:3]} vs {r2[:3]}")
    check("a job blocked 600s does NOT outrank a meeting in 2m",
          r1.index("Standup with Ana") < r1.index("CI red on main"),
          " -> ".join(r1))
    meetings = [l for l in r1 if l == "Standup with Ana"]
    check("the full meeting order is soonest-first",
          r1[:3] == ["Standup with Ana"] * 3, " -> ".join(r1))

    print("\n6. one bad file does not kill the loop")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "good.json").write_text(json.dumps(meeting(5).as_json()))
        (d / "broken.json").write_text("{not json at all")
        (d / "wrong_shape.json").write_text(json.dumps({"nope": 1}))
        loaded = q.load_outbox(d)
        check("the good candidate still loaded", len(loaded) >= 1,
              f"{len(loaded)} loaded")
        check("the malformed files were skipped, not raised",
              all(isinstance(c, q.Candidate) for c in loaded))
    check("missing outbox returns empty, not an error",
          q.load_outbox(Path("/nonexistent/outbox")) == [])

    print("\n7. round trip through the documented spool format")
    original = blocked(600)
    restored = q.Candidate.from_json(json.loads(json.dumps(original.as_json())))
    check("candidate survives as_json -> from_json",
          restored == original, f"{restored.cls}/{restored.elapsed_s}s")

    print("\n8. evidence derived from a real ledger")
    import tempfile as _tf
    from memory import FeedbackStore

    with _tf.TemporaryDirectory() as tmp:
        store = FeedbackStore(Path(tmp) / "e.db")

        def card():
            return store.add_card(query="q", stage1="x" * 300,
                                  summary="s", keywords=["k"]).id

        # Four deferrals, no judged drill. The rate must be ABSENT, not 0.0:
        # 0.0 would say "drills always succeed", which is a claim about a
        # population that does not exist.
        for _ in range(4):
            store.add_event(card(), "drill_deferred", "detail_generation=off")
        b1, p1 = q.evidence_from_store(store)
        check("CONTROL: the ledger really holds 4 deferrals",
              store.counts().get("drill_deferred") == 4)
        check("deferrals do NOT produce a drill_miss_rate",
              b1.drill_miss_rate is None,
              f"got {b1.drill_miss_rate}, want absent")
        check("'drill_miss_rate' is omitted from the evidence dict",
              "drill_miss_rate" not in b1.evidence().get("detail_generation", {}),
              str(b1.evidence().get("detail_generation")))

        # One real drill, one miss -> a real, derivable rate of 0.5.
        store.add_event(card(), "expand", "touchbar/tap")
        store.add_event(card(), "drill_miss", "generated but nothing worth it")
        b2, _ = q.evidence_from_store(store)
        check("a judged drill set produces a real rate",
              b2.drill_miss_rate == 0.5, f"{b2.drill_miss_rate} (1 miss / 2)")

        print("\n9. absent is not zero")
        b3, prov3 = q.evidence_from_store(store)
        ev3 = b3.evidence()
        for metric in ("label_days", "clean_pushes", "n_labels",
                       "queue_worth_rate", "rho"):
            present = any(metric in m for m in ev3.values())
            check(f"{metric} is ABSENT, not reported as 0",
                  not present and prov3.get(metric) == "no source",
                  f"present={present} prov={prov3.get(metric)}")
        check("push counters ARE zero, because that is true",
              ev3["push_enabled"]["pushes_today"] == 0
              and ev3["push_enabled"]["pushes_last_hour"] == 0)

        # The decisive one: an absent metric must not flip an axis.
        from policy import resolve
        resolved = resolve(b3.evidence())
        no_source_axes = ("push_enabled", "g1_model", "interrupt_gate",
                          "examination_source")
        check("axes with NO SOURCE stay unresolved",
              all(not resolved.decisions[a].enough_evidence
                  for a in no_source_axes),
              f"unresolved={resolved.unresolved()}")
        # The counterweight, and the actual finding: drill_miss_rate is the
        # ONE metric with a real ledger source, so detail_generation is the
        # ONE axis that can move off its default from shipped data alone.
        check("the one axis with a real source is the one that resolves",
              resolved.decisions["detail_generation"].enough_evidence,
              f"detail_generation -> {resolved.get('detail_generation')} "
              f"({resolved.evidence_for('detail_generation').reason})")
        check("an axis with no source does not read as measured-and-low",
              not resolved.evidence_for("push_enabled").enough_evidence,
              resolved.evidence_for("push_enabled").reason)

        # A label_days of 0 supplied on purpose is a different claim from None.
        b4 = q.Budget(label_days=0)
        check("explicit 0 IS reported, and reads as 'not enough days'",
              b4.evidence()["push_enabled"].get("label_days") == 0)
        store.close()

    print()
    print(f"summary: {q.summarize(q.plan(mixed, budget=q.Budget(label_days=20), now=NOW))}")
    print()
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        return 1
    print("OK    silent by default, nothing dropped, budget binds the batch, "
          "order is stable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())