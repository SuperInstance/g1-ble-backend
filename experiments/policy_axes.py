#!/usr/bin/env python3
"""
Every contested decision resolves from evidence, never from opinion.

Also proves the safety property that matters: with NO evidence at all, every
axis lands on its conservative default.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import policy  # noqa: E402
from policy import POLICY_AXES, resolve  # noqa: E402

SCENARIOS = [
    ("fresh install, nothing measured", {}),
    ("gaze calibrated clean (rho=2.1)", {"examination_source": {"rho": 2.1}}),
    ("gaze dirty (rho=0.9)", {"examination_source": {"rho": 0.9}}),
    ("200 labels, queue worth 0.7", {"g1_model": {"n_labels": 200, "queue_worth_rate": 0.7}}),
    ("200 labels, queue worth 0.2", {"g1_model": {"n_labels": 200, "queue_worth_rate": 0.2}}),
    ("320 clean pushes", {"interrupt_gate": {"clean_pushes": 320}}),
    ("40 clean pushes", {"interrupt_gate": {"clean_pushes": 40}}),
    ("taps routinely find nothing", {"detail_generation": {"drill_miss_rate": 0.44}}),
    ("14 days of tier labels", {"push_enabled": {"label_days": 14}}),
]

def main() -> int:
    print("AXES")
    print(f"  {len(POLICY_AXES)} contested questions, each with a named deciding metric")
    for a in POLICY_AXES:
        print(f"    {a.id:<20} {'|'.join(a.options):<26} "
              f"default={a.default}")

    fails = 0
    print("\nRESOLUTION BY EVIDENCE")
    for label, ev in SCENARIOS:
        p = resolve(ev)
        active = {k: v.option for k, v in p.decisions.items()
                  if v.option != next(a.default for a in POLICY_AXES if a.id == k)}
        pending = p.unresolved()
        print(f"\n  {label}")
        print(f"    changed from default: {active or '(none)'}")
        print(f"    still undecided     : {pending or '(none)'}")

    # Safety property: with nothing measured, nothing may be switched on.
    p = resolve({})
    unsafe = [k for k, v in p.decisions.items()
              if v.option not in {a.default for a in POLICY_AXES if a.id == k}]
    if unsafe:
        fails += 1
        print(f"\nFAILED: axes changed with no evidence: {unsafe}")
    else:
        print("\n  SAFETY: with zero evidence every axis holds its conservative "
              "default. No axis was switched on by opinion.")

    # Budget is a CONSTRAINT evaluated inside the predicate, not downstream.
    # Evidence and budget are different kinds of input; before this, a spent
    # budget could not stop an axis that evidence had already turned on.
    print()
    budget_cases = [
        ({"label_days": 30}, "on", "30 days of labels, budget clear"),
        ({"label_days": 30, "pushes_today": 4}, "off", "4/4 daily used"),
        ({"label_days": 30, "pushes_last_hour": 2}, "off", "2/2 hourly used"),
        ({"label_days": 3, "pushes_today": 4}, "off",
         "budget spent before milestone even completes"),
    ]
    for ev, want, label in budget_cases:
        got = resolve({"push_enabled": ev}).get("push_enabled")
        ok = got == want
        fails += not ok
        print(f"  BUDGET {label:<44} -> {got!r} {'ok' if ok else f'FAIL want {want!r}'}")

    # Manual override must be honoured and validated.
    p = resolve({}, {"g1_model": "on"})
    ok = p.get("g1_model") == "on" and "override" in p.decisions["g1_model"].reason
    fails += not ok
    print(f"  OVERRIDE honoured: {ok}")
    try:
        resolve({}, {"g1_model": "banana"})
        fails += 1
        print("  VALIDATION: FAILED to reject a bad option")
    except ValueError:
        print("  VALIDATION: bad option rejected")

    # An override must not read as a measurement.
    #
    # `--set-policy push_enabled=on` used to produce Decision(..., True), so
    # the axis vanished from Policy.unresolved() and any gate keyed on "is
    # this settled?" would accept a human assertion as evidence. The pin
    # persists in policy.json, so the failure mode is a test switch outliving
    # the test session: flip an axis to watch something happen, forget, and
    # find weeks later that a live process has been running on the pin.
    p = resolve({}, {"push_enabled": "on"})
    manual = p.decisions["push_enabled"]
    ok = manual.manual and not manual.enough_evidence
    fails += not ok
    print(f"  OVERRIDE is not evidence: manual={manual.manual} "
          f"enough={manual.enough_evidence} "
          f"{'ok' if ok else 'FAIL'}")

    ok = p.manual() == ["push_enabled"] and not p.settled_by_evidence("push_enabled")
    fails += not ok
    print(f"  manual() names it: {p.manual()} "
          f"{'ok' if ok else 'FAIL'}")

    # Control: an axis settled BY EVIDENCE must still count, or the fix above
    # would be "never trust the policy", which is not the claim.
    settled = resolve({"push_enabled": {"label_days": 20}})
    ok = settled.settled_by_evidence("push_enabled")
    fails += not ok
    print(f"  CONTROL: evidence-settled axis still counts: {ok}")

    # And a pin must be able to lapse on its own.
    import tempfile
    import time as _time
    with tempfile.TemporaryDirectory() as tmp:
        orig = policy.policy_path
        policy.policy_path = lambda: Path(tmp) / "policy.json"
        try:
            policy.save_overrides({"push_enabled": "on"}, ttl_s=0.2)
            alive = policy.load().get("push_enabled")
            _time.sleep(0.35)
            lapsed = policy.load().get("push_enabled")
            ok = alive == "on" and lapsed == "off"
            fails += not ok
            print(f"  TTL expires unattended: {alive!r} -> {lapsed!r} "
                  f"{'ok' if ok else 'FAIL'}")
        finally:
            policy.policy_path = orig

    print(f"\n{'FAILED' if fails else 'passed'}: {fails} problem(s)")
    return 1 if fails else 0

if __name__ == "__main__":
    raise SystemExit(main())
