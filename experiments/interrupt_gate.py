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
import time
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


def meeting(mins: float, accepted: bool = True, *,
            ts=None, line: str = "Standup with Ana") -> q.Candidate:
    return q.Candidate(source="cal", cls="meeting", line=line,
                       ts=NOW if ts is None else ts, subject="meeting",
                       accepted=accepted, minutes_until=mins)


def blocked(seconds: int, is_blocked: bool = True, *,
            ts=None) -> q.Candidate:
    return q.Candidate(source="build", cls="blocked_job", line="CI red on main",
                       ts=NOW if ts is None else ts, subject="infra",
                       blocked=is_blocked, elapsed_s=seconds)


def device(pct, *, ts=None) -> q.Candidate:
    meta = {} if pct is None else {"battery_pct": pct}
    return q.Candidate(source="glasses", cls="device", line="Battery low",
                       ts=NOW if ts is None else ts, subject="glasses",
                       meta=meta)


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
            # `startswith` because a provenance string may carry a reason
            # after the verdict ("no source: needs the calibration walk").
            check(f"{metric} is ABSENT, not reported as 0",
                  not present
                  and str(prov3.get(metric, "")).startswith("no source"),
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

    print("\n10. the simulator's own spool format parses and decides")
    # Contract test. `sim/events.py` has always written the documented spool
    # format with the key "class"; this module's parser read "cls". Every
    # simulated candidate therefore parsed as class "unknown" and failed G2
    # eligibility forever, with no exception and no log line -- a queue that
    # silently never pushes looks exactly like a conservative one.
    #
    # The rule this enforces: if two modules agree on a contract, the contract
    # itself needs a test. Round-tripping the real producer is the only check
    # that would ever have noticed.
    from sim.events import Candidate as SimCandidate

    sim_cand = SimCandidate(
        cid="c1", source="cal", cls="meeting", line="Standup with Ana",
        ts=NOW, subject="meeting", keywords=("meeting",),
        accepted=True, minutes_until=4.0,
    )
    parsed = q.Candidate.from_json(sim_cand.as_spool_json())
    check("sim's 'class' key is read, not defaulted to unknown",
          parsed.cls == "meeting", f"got {parsed.cls!r}")
    check("the simulated meeting is eligible under G2",
          parsed.eligible(NOW)[0], parsed.eligible(NOW)[1])

    sim_blocked = SimCandidate(
        cid="c2", source="build", cls="blocked_job", line="CI red",
        ts=NOW, subject="infra", blocked=True, elapsed_s=400,
    )
    parsed_b = q.Candidate.from_json(sim_blocked.as_spool_json())
    check("the simulated blocked job is eligible too",
          parsed_b.eligible(NOW)[0], parsed_b.eligible(NOW)[1])

    # And end to end: a whole simulated day through the real decision.
    verds = q.plan([parsed, parsed_b], budget=q.Budget(label_days=20), now=NOW)
    check("a simulated day produces real pushes through the real decision",
          sum(v.is_push for v in verds) == 2,
          f"{sum(v.is_push for v in verds)} of {len(verds)}")

    # Sim's eligibility rule and the shipped G2 must agree, or the harness is
    # measuring a different product from the one interrupt.py implements.
    check("sim's own eligibility agrees with G2 on a meeting",
          sim_cand.is_eligible_to_push == parsed.eligible(NOW)[0])
    check("sim's own eligibility agrees with G2 on a blocked job",
          sim_blocked.is_eligible_to_push == parsed_b.eligible(NOW)[0])

    print("\n11. the budget is counted, not accumulated")
    # The failure this prevents: Budget.spend() only incremented, so in a
    # long-running process pushes_last_hour would reach 2 and stay there --
    # a daemon that pushed twice and then went silent forever, with a log
    # full of successes and no error anywhere. A silent daemon is worse than
    # a broken one, because nobody goes looking for it.
    #
    # Timestamps here are relative to the REAL clock, because
    # evidence_from_store() reads the wall time and a synthetic 2026 stamp
    # would sit outside the window by however long the suite has been running.
    import time as _t
    with _tf.TemporaryDirectory() as tmp:
        bstore = FeedbackStore(Path(tmp) / "push.db")
        t0 = _t.time()
        # two recent, one 90 minutes old, one two days old
        for offset, cls in ((60, "meeting"), (120, "device"),
                            (5400, "meeting"), (172800, "meeting")):
            bstore.record_push(cls, source="cal", text="x", ts=t0 - offset)

        hh, dd = bstore.push_counts()
        check("hourly count ignores pushes outside the window",
              hh == 2, f"{hh} (want 2)")
        check("daily count ignores a push from a previous day",
              dd == 3, f"{dd} (want 3)")

        b, prov_b = q.evidence_from_store(bstore)
        check("the evidence bridge reports both as derived",
              prov_b.get("pushes_last_hour") == "derived"
              and prov_b.get("pushes_today") == "derived")
        check("the bridge carries the real counts through",
              b.pushes_last_hour == 2 and b.pushes_today == 3,
              f"{b.pushes_last_hour}/{b.pushes_today}")

        from policy import resolve
        spent = resolve(b.evidence())
        check("a spent budget makes the policy refuse to push",
              spent.get("push_enabled") == "off",
              spent.evidence_for("push_enabled").reason)

        # THE assertion. An hour later the hourly budget must come BACK.
        # An increment-only counter cannot decrease, so this is the line that
        # fails if the construct regresses.
        hh_later, dd_later = bstore.push_counts(now=t0 + 3600)
        check("an hour later the hourly budget is available again",
              hh_later == 0, f"{hh_later} (want 0 -- both recent pushes aged out)")
        check("the daily budget survives within the same day",
              dd_later == 3, f"{dd_later} (want 3)")

        # Intent is recorded BEFORE the write, so a crash between deciding and
        # sending still spends the budget.
        row = bstore.record_push("meeting", source="cal", text="crashy")
        before, _ = bstore.push_counts()
        bstore.settle_push(row, "transport_error")
        after, _ = bstore.push_counts()
        check("a FAILED attempt still spends the budget",
              before == after == 3,
              f"{before} before settle, {after} after -- budget is spent on "
              f"ATTEMPT, not on success")
        bstore.close()
    print("\n12. the whole loop closes: decide -> record -> label -> evidence")
    # The point: the policy layer has never had an input it did not have to
    # fake. n_labels / label_days / queue_worth_rate all read "no source" until
    # somebody labels something, and this is the path that makes them derived.
    with _tf.TemporaryDirectory() as tmp:
        loop_store = FeedbackStore(Path(tmp) / "loop.db")
        # Distinct arrival times. Two meetings with the same text are two
        # events because they arrived at different moments; the ledger's dedup
        # key is (source, class, line, ts), so sharing one timestamp would make
        # them one row. That is correct behaviour on real data and a lie in a
        # fixture, so the fixture now says what it means.
        verds = q.plan([meeting(5), blocked(600), device(4),
                        meeting(99, ts=NOW + timedelta(minutes=1))],
                       budget=q.Budget(label_days=20), now=NOW)
        before, _ = q.evidence_from_store(loop_store)
        check("CONTROL: with nothing labelled, the label metrics are ABSENT",
              before.n_labels is None and before.queue_worth_rate is None,
              f"n_labels={before.n_labels} worth={before.queue_worth_rate}")

        for v in verds:
            q.record_verdict(loop_store, v, dry_run=True)
        pending = loop_store.unlabelled()
        check("every decision is queued for review",
              len(pending) == len(verds), f"{len(pending)}/{len(verds)}")

        # Label EXPLICITLY rather than by position. The first version took the
        # first three rows and asserted a rate of 2/3, which was wrong:
        # rank() orders by urgency, so the ineligible meeting(99) lands second
        # and the positional slice picked a different set than assumed. The
        # metric was right; the test's picture of the ordering was not.
        queued_rows = [r for r in pending if r["decision"] == "queue"]
        pushed_rows = [r for r in pending if r["decision"] == "push"]
        for row in queued_rows:
            loop_store.set_label(row["id"], 0)      # queue was not worth it
        for row in pushed_rows:
            loop_store.set_label(row["id"], 1)      # pushes were fine

        after, _ = q.evidence_from_store(loop_store)
        stats_after = loop_store.label_stats()
        check("after labelling, the metrics become DERIVED, not absent",
              after.n_labels == len(pending) and after.queue_worth_rate is not None,
              f"n_labels={after.n_labels} worth={after.queue_worth_rate}")
        check("queue_worth_rate is the mean over QUEUED rows ONLY",
              after.queue_worth_rate == 0.0
              and stats_after['queue_labelled'] == len(queued_rows)
              # push_labelled counts REAL sends only, so the dry-run pushes
              # correctly contribute 0. That is the assertion.
              and stats_after['push_labelled'] == 0
              and len(pushed_rows) > 0,
              f"worth={after.queue_worth_rate} from "
              f"{stats_after['queue_labelled']} "
              f"queued rows; {len(pushed_rows)} dry-run pushes correctly "
              f"contributed 0")
        check("clean_pushes still ABSENT -- none of these pushes was the "
              "result of a real send",
              after.clean_pushes is None, f"{after.clean_pushes}")

        censored = q.Verdict(meeting(5, ts=NOW + timedelta(minutes=2)),
                            "push", "eligible", "eligible")
        q.record_verdict(loop_store, censored, observable=False)
        check("a push made with the link down is recorded unlabelable",
              any(not r["observable"] for r in loop_store.unlabelled()),
              "censoring applies to PUSHES, not to terminal review")
        loop_store.close()

    print("\n13. re-recording the same event does not manufacture ground truth")
    # This suite measures the review loop, so an inflated ledger is a broken
    # measuring instrument, not a cosmetic bug. `n_labels` is the numerator
    # behind g1_model's >=100 volume gate: if replaying one spool file three
    # times yields three times the labels, then re-running a dry run walks the
    # system toward switching itself on with nothing new observed. Every number
    # still looks like a count, which is why it needs its own test.
    #
    # The first fix threaded a dedup_key through record_decision but left
    # record_verdict calling it WITHOUT a timestamp, so the key fell back to
    # time.time() and was different on every run. 3 runs -> 15 rows for 5
    # candidates. The unique index was there and correct; nothing ever hit it.
    #
    # And it was WORSE than a plain bug, which the first version of this test
    # nearly shipped. The fallback key is round(time.time(), 3) -- MILLISECOND
    # precision. Nine back-to-back writes all land inside one millisecond, so
    # they share a key and the dedup appears to work; a tenth write that
    # straddles a boundary suddenly produces a new row. Measured here with the
    # fix reverted: four rapid writes -> [1, 1, 1, 1], and one that crossed the
    # boundary -> [1, 1, 1, 2]. Whether duplicates appeared depended on how fast
    # the disk was. That is the failure mode this whole project is about, so
    # the test below pins the clock instead of racing it.
    with _tf.TemporaryDirectory() as tmp:
        idem = FeedbackStore(Path(tmp) / "idem.db")
        cands = [meeting(5), blocked(600), device(4)]
        first = q.plan(cands, budget=q.Budget(label_days=20), now=NOW)
        ids_run1 = [q.record_verdict(idem, v, dry_run=True) for v in first]
        rows_after_1 = len(idem.unlabelled())

        # Deterministic assertion: the row's own timestamp is the EVENT's,
        # not the moment of writing. This is what fails loudly when the fix is
        # removed, regardless of how fast the writes happen to be.
        stored = idem._conn.execute(
            "SELECT created_at FROM decisions WHERE id = ?", (ids_run1[0],)
        ).fetchone()["created_at"]
        check("the ledger stores the EVENT's timestamp, not the write time",
              abs(stored - NOW.timestamp()) < 1e-6,
              f"created_at={stored} vs event={NOW.timestamp()} "
              f"(a wall clock would be within seconds of this run and "
              f"different on the next, which is the whole defect)")

        # Replay the identical spool, twice more, as an operator would --
        # sleeping long enough that a wall-clock key would be guaranteed to
        # differ, which is what made the original bug intermittent.
        time.sleep(0.01)              # > 1ms: a ms-quantised key would differ
        ids_run2 = [q.record_verdict(idem, v, dry_run=True) for v in first]
        time.sleep(0.01)
        ids_run3 = [q.record_verdict(idem, v, dry_run=True) for v in first]
        rows_after_3 = len(idem.unlabelled())

        check("CONTROL: three replays of one spool still yield one row each",
              rows_after_1 == len(first) == rows_after_3,
              f"{rows_after_1} -> {rows_after_3} rows for {len(first)} "
              f"candidates")
        check("and they are the SAME rows, not merely the same count",
              ids_run1 == ids_run2 == ids_run3,
              f"{ids_run1} vs {ids_run2} vs {ids_run3}")

        # A human answered row ids_run1[0]. A later automatic pass must not
        # erase that: it is the only ground truth in the system. The labelled
        # row also leaves the review queue, so the expected count drops by one
        # and must stay there -- a replay that resurrected it would show up as
        # the count going back UP, which is the silent failure.
        idem.set_label(ids_run1[0], 1)
        q.record_verdict(idem, first[0], dry_run=True)
        rows = idem.unlabelled()
        check("a replay preserves the human's label",
              len(rows) == rows_after_3 - 1
              and all(r["id"] != ids_run1[0] for r in rows),
              f"{len(rows)} unlabelled rows; the labelled row {ids_run1[0]} "
              f"stays answered and is not re-queued")

        # A CHANGED action is still the same event. It must update in place,
        # so it stays one item of evidence rather than becoming two. Uses row 1,
        # which is deliberately still unlabelled so it is visible in the queue.
        flipped = q.Verdict(first[1].candidate, "push", "eligible", "forced")
        q.record_verdict(idem, flipped, dry_run=True)
        rows = idem.unlabelled()
        check("a decision that CHANGES updates in place, not as a new row",
              len(rows) == rows_after_3 - 1
              and any(r["id"] == ids_run1[1] and r["decision"] == "push"
                      for r in rows),
              f"{len(rows)} rows; row {ids_run1[1]} is now "
              f"{[r['decision'] for r in rows if r['id'] == ids_run1[1]]}")

        # Finally the direct test of the actual defect: two events that differ
        # ONLY by timestamp must stay distinct, which only works if the EVENT
        # time is in the key rather than the wall clock.
        hour_later = (NOW + timedelta(hours=1)).timestamp()
        idem.record_decision(decision="queue", cls="meeting", source="cal",
                             line="Standup with Ana", ts=hour_later)
        n_new = len(idem.unlabelled())
        check("two events differing only in time stay two rows",
              n_new == rows_after_3,
              f"{n_new} rows; a later event with identical text is its own row")
        idem.record_decision(decision="queue", cls="meeting", source="cal",
                             line="Standup with Ana", ts=hour_later)
        check("...and a later replay of the NEW event still folds in",
              len(idem.unlabelled()) == n_new,
              "replaying the new event did not duplicate it either")
        idem.close()

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