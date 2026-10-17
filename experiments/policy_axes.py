#!/usr/bin/env python3
"""
Every contested decision resolves from evidence, never from opinion.

Also proves the safety property that matters: with NO evidence at all, every
axis lands on its conservative default.
"""
import json
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

    # A TTL is the mitigation, not the fix. It is opt-in, so the DEFAULT pin is
    # permanent -- and there was no way to unpin from the CLI at all, only by
    # editing policy.json by hand. I hit exactly this while testing: pinning
    # push_enabled to reproduce a queue bug, then having to hand-edit the file
    # to get back to a conservative posture.
    with tempfile.TemporaryDirectory() as tmp:
        orig = policy.policy_path
        policy.policy_path = lambda: Path(tmp) / "policy.json"
        try:
            policy.save_overrides({"push_enabled": "on", "g1_model": "on"},
                                  ttl_s=60.0)
            path, removed = policy.clear_overrides(["g1_model"])
            left = policy.load()
            ok = (removed == ["g1_model"] and left.get("g1_model") == "off"
                  and left.get("push_enabled") == "on")
            fails += not ok
            print(f"  clearing one axis leaves the other: g1_model -> "
                  f"{left.get('g1_model')!r}, push_enabled stays "
                  f"{left.get('push_enabled')!r} {'ok' if ok else 'FAIL'}")

            # A TTL that survives must still govern something. Carrying
            # set_at/ttl_s across a clear that removed its only override
            # leaves an expiry with nothing to expire.
            raw = json.loads(path.read_text())
            ok = "set_at" in raw and raw.get("ttl_s") == 60.0
            fails += not ok
            print(f"  a partial clear keeps the TTL for what survives "
                  f"{'ok' if ok else 'FAIL'}")

            # g1_model was already removed by the partial clear above, so
            # "what was removed" is just what was still pinned. Asserting the
            # full list here would be asserting a duplicate.
            path, removed = policy.clear_overrides()
            raw = json.loads(path.read_text())
            ok = (removed == ["push_enabled"]
                  and raw["overrides"] == {}
                  and "set_at" not in raw and "ttl_s" not in raw)
            fails += not ok
            print(f"  clearing all drops the now-meaningless TTL too "
                  f"{'ok' if ok else 'FAIL'}")

            # An unknown axis must be REFUSED, not silently ignored: reporting
            # success for a pin that was never removed is the same lie as
            # "cleared nothing" while looking like it worked.
            try:
                policy.clear_overrides(["g2_model"])
                refused = False
            except ValueError:
                refused = True
            fails += not refused
            print(f"  an unknown axis is refused, not ignored "
                  f"{'ok' if refused else 'FAIL'}")

            # An existing-but-empty policy file is a legitimate no-op: the
            # operator asked to clear, there was nothing, and the command says
            # so rather than raising. Only a genuinely ABSENT file is an
            # error, because that means there is no policy file to speak of.
            _, removed_again = policy.clear_overrides()
            ok = removed_again == []
            fails += not ok
            print(f"  clearing an empty policy is a clean no-op "
                  f"{'ok' if ok else 'FAIL'}")

            path.unlink()
            try:
                policy.clear_overrides()
                absent_ok = False
            except FileNotFoundError:
                absent_ok = True
            fails += not absent_ok
            print(f"  a missing policy file says so rather than creating one "
                  f"{'ok' if absent_ok else 'FAIL'}")
            ok = not path.exists()
            fails += not ok
            print(f"  ...and does not silently create it "
                  f"{'ok' if ok else 'FAIL'}")
        finally:
            policy.policy_path = orig

    print(f"\n{'FAILED' if fails else 'passed'}: {fails} problem(s)")
    return 1 if fails else 0

if __name__ == "__main__":
    raise SystemExit(main())
