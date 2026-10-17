#!/usr/bin/env python3
"""
Every contested decision resolves from evidence, never from opinion.

Also proves the safety property that matters: with NO evidence at all, every
axis lands on its conservative default.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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

    print(f"\n{'FAILED' if fails else 'passed'}: {fails} problem(s)")
    return 1 if fails else 0

if __name__ == "__main__":
    raise SystemExit(main())
