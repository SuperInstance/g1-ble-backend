"""
Can the system be stopped, and does a bad source get quietened?

Two rules from the design that were written down and never built:

  documents/07: "one mis-fire auto-suspends that source class for 24 hours
                 -- not three. Trust collapses on the second bad
                 interruption."
  documents/13: "one `n` in review suspends that push type for 24 h."

They are built here as pure state machines with no BLE and no LED. They are
shipped dormant -- reachable, callable, tested -- because a rule that is
neither wired nor tested is indistinguishable from a rule that does not exist,
and `experiments/wiring_audit.py` exists precisely to stop that ambiguity from
hiding.

THE ANTI-STARVATION CONSTRAINT IS THE WHOLE DESIGN PROBLEM

The panel also wrote, in documents/07, that signal starvation is what kills
this product: a wearer who is interrupted too much stops reading anything. So
a suspend mechanism is dangerous by construction, and the naive version of it
is a way to mute yourself permanently.

Three rules follow, and each is asserted below:

  1. SUSPENSION IS NEVER TRIGGERED BY ABSENCE. Only an explicit negative acts.
     A source class whose pushes nobody examined is never suspended, because
     not-looking is not evidence of anything and habituation means an unseen
     push says even less. This is the single most important property: it is
     what stops a quiet class from being starved into silence forever.
  2. SUSPENSION EXPIRES, AND EXPIRY IS NOT CONDITIONAL ON EVIDENCE. There is no
     "stay suspended until a clean push is seen" rule. That is a starvation
     engine -- the class stays muted precisely because nothing is happening.
  3. SUSPENDED DOES NOT MEAN DROPPED. A suspended candidate is INELIGIBLE,
     and ineligible already means queued. The queue stays the safety net.

A fourth property is a refusal rather than a feature: a class suspension
never escalates to a global mute. Silence is already the default posture, so
global escalation removes information and buys nothing.

WHAT COUNTS AS A MIS-FIRE, and what deliberately does not:

  explicit dismiss on a pushed item  -> YES, the trigger. It is the only
      intentional negative the wearer has, and it costs them one gesture they
      were going to make anyway.
  never examined                     -> NO. See rule 1.
  delivery failure (no ack, one arm) -> NO. That is a transport problem. It
      routes to link health, not to a class penalty. Conflating them lets a
      flaky radio mute meetings, battery alerts AND blocked jobs at once,
      which is the mechanism by which an auto-suspend rule becomes a global
      mute by accident.

THE 24-HOUR NUMBER IS A CHOICE, NOT A CONSTANT

`sim/wearer.py` models trust as decaying with bad interruptions and recovering
at 0.03/day, and `sim/README.md` records that the simulator *deliberately*
violates this 24-hour rule -- "this is the design assumption I violate most
directly". So 24h is a design decision the panel argued for, not something
derived from data. It is a named constant here for the same reason the
conservative defaults in policy.py are named: so it can be argued with.

Run: ``.venv/bin/python experiments/suspend_gate.py``
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import interrupt as q                                  # noqa: E402
from memory import FeedbackStore                       # noqa: E402

FAILURES: list[str] = []
SUSPEND_HOURS = 24.0
NOW = 1_800_000_000.0        # fixed clock, so nothing depends on wall time


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


def cand(cls: str = "meeting", source: str = "cal") -> q.Candidate:
    from datetime import datetime, timezone
    return q.Candidate(source=source, cls=cls, line="x",
                       ts=datetime.fromtimestamp(NOW, timezone.utc),
                       subject="s", minutes_until=4.0)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = FeedbackStore(Path(tmp) / "s.db")

        print("1. the kill-switch")
        # A file, re-read every cycle, so a process that has stopped
        # responding to its terminal can still be stopped. The alternative --
        # a gesture on the TouchBar -- depends on 0xF5 decode correctness,
        # which is unverified, so the gesture path is not built yet.
        kill = Path(tmp) / "armed"
        check("disarmed by default -- no file means no pushing",
              not q.kill_switch_armed(kill), "absent file reads as disarmed")
        kill.write_text("armed")
        check("armed once the file says so", q.kill_switch_armed(kill))

        print("\n2. suspension is only ever triggered by an explicit negative")
        s0 = q.suspension_state(store)
        check("nothing suspended to start with", s0 == {})

        # THE anti-starvation property, stated as a test. 200 pushes, nobody
        # looks at any of them. Under rule 1 nothing may be suspended.
        for i in range(200):
            row = store.record_push("meeting", source="cal",
                                    text=f"unseen {i}", ts=NOW - i)
            store.settle_push(row, "delivered")
        store.note_unexamined("meeting", count=200, ts=NOW)
        s1 = q.suspension_state(store)
        check("200 never-examined pushes suspend NOTHING",
              s1 == {}, f"{s1} -- absence is not evidence")

        # A transport failure is not a verdict on the class.
        for i in range(50):
            row = store.record_push("device", source="glasses",
                                    text=f"failed {i}", ts=NOW - i)
            store.settle_push(row, "no_ack")
        s2 = q.suspension_state(store)
        check("50 undelivered pushes suspend NOTHING",
              s2 == {}, f"{s2} -- a dead radio is not a bad source")

        # One explicit refusal, by contrast, does count.
        store.record_misfire("meeting", source="cal", detail="dismiss",
                             ts=NOW)
        s3 = q.suspension_state(store)
        check("ONE explicit refusal suspends that class",
              "meeting" in s3 and "device" not in s3,
              f"suspended={sorted(s3)}")

        print("\n3. the expiry is real and unconditional")
        later = q.suspension_state(store, now=NOW + SUSPEND_HOURS * 3600 + 1)
        check("suspension lapses after 24h",
              later == {}, f"{later}")

        still = q.suspension_state(store, now=NOW + 60)
        check("suspended one minute later", "meeting" in still)

        # Rule 2: a quiet class must not be re-suspended forever by the mere
        # passage of time. A second mis-fire restarts the clock -- that is the
        # only thing that can.
        store.record_misfire("meeting", source="cal", ts=NOW + 61)
        check("a SECOND refusal restarts the 24h",
              "meeting" in q.suspension_state(store, now=NOW + 7200),
              "expiry is from the latest mis-fire, not the first")

        print("\n4. suspended is not dropped")
        # This is the invariant, and it needs a budget with NOTHING spent --
        # the first version of this check reused a budget that still had two
        # pushes on it, so everything queued on the budget gate and the
        # suspension gate was never reached. A test that passes without
        # exercising the thing it names is the failure mode this project
        # keeps finding, so it is worth being explicit about.
        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(NOW, timezone.utc)

        # Control first: with nothing suspended, an eligible meeting pushes.
        ctrl = q.plan([cand("meeting")], budget=q.Budget(label_days=20),
                      now=now_dt)
        check("CONTROL: unsuspended, the same candidate PUSHES",
              ctrl and ctrl[0].is_push,
              f"{ctrl[0].action}/{ctrl[0].gate if ctrl else 'none'}")

        # Now with the class suspended.
        sus = q.plan([cand("meeting")], budget=q.Budget(label_days=20),
                     store=store, now=now_dt)
        check("suspended candidate is QUEUED",
              sus and sus[0].action == "queue",
              f"{sus[0].action if sus else 'none'}")
        check("and the gate that queued it is 'suspended', not the budget",
              sus and sus[0].gate == "suspended",
              f"gate={sus[0].gate if sus else 'none'} -- if this says "
              f"budget/axis the suspension gate was never reached")
        check("the reason names the class and the time",
              sus and "meeting" in sus[0].reason and "suspended until" in sus[0].reason,
              sus[0].reason if sus else "")

        print("\n5. no escalation to a global mute")
        # A suspended class plus a kill switch must not, between them, be able
        # to produce a state where nothing is EVER eligible again.
        s = q.suspension_state(store)
        check("suspension is per-class, not global",
              set(s).issubset({"meeting", "blocked_job", "device"}),
              f"suspended classes: {sorted(s)}")
        eligible_other = [cl for cl in ("blocked_job", "device")
                          if cl not in s]
        check("other classes remain interruptible while one is suspended",
              len(eligible_other) == 2,
              f"{eligible_other} still eligible")

        store.close()

    print()
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        return 1
    print("OK    stoppable, one refusal mutes one class for 24h, nothing dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())