#!/usr/bin/env python3
"""
Independently reproduce Experiment B's baseline number, and exercise the
gate that now lives in pipeline.py.

Experiment B reported the regex baseline at AUC 0.688 on a 50-item set it
built itself. That set is not in the repo, so this does NOT reproduce their
number -- it checks the implementation is coherent and the failure modes it
described are real, on cases written here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (  # noqa: E402
    check_glanceable, glanceability_score, repair_buried_fact,
    enforce_display_budget, fits_display,
)

CASES = [
    # (summary, should_pass, why)
    ("Train 8814 Osaka delayed to 09:40, platform 6.", True, "concrete lead"),
    ("Rain starting now. Umbrella is in the hall closet.", True, "no digits, still concrete"),
    ("Stripe Charges API removed 2026-11-01; migrate to PaymentIntents.", True, "name + date"),
    ("This was fixed in the latest release.", False, "bare referent"),
    ("It has been resolved for you now.", False, "bare referent"),
    ("Consider reviewing the situation when convenient.", False, "pure connective tissue"),
    ("Regarding case 4471, there are some points to discuss later.", False, "vague + lead"),
    ("The situation has been addressed appropriately for your request.", False, "empty prose"),
]

BURIED = [
    "The migration is scheduled. API keys rotate 2026-11-01 and old ones die.",
    "Everything is handled already. The server room hit 31C last night.",
    "Nothing to do right now. Your password expires in 2 hours.",
]

def main() -> int:
    fails = 0
    print("glanceability gate (blocking check)")
    print(f"  {'ok?':<5} {'score':>6} {'want':<5} {'verdict':<7} text")
    for text, want, why in CASES:
        r = check_glanceable(text)
        good = r.ok == want
        fails += not good
        print(f"  {str(r.ok):<5} {r.score:6.2f} {str(want):<5} "
              f"{'OK' if good else 'MISS':<7} {text[:44]}")
        if not good:
            print(f"        reason: {r.reason} ({why})")

    print("\nburied-fact repair (deterministic reorder)")
    # The premise is the wearer's, not the test's: after repair the concrete
    # token must land inside the first ~48 characters (two 24-char lines).
    # Checking the first *word* is wrong -- the reordered fact can itself
    # legitimately begin with "The".
    import re as _re
    for text in BURIED:
        token = _re.search(r"\d[\d,.:\-]*", text)
        want_head = token.group(0)[:20] if token else ""
        fixed = repair_buried_fact(text)
        head = fixed[:48]
        ok = (want_head in head) and fits_display(fixed) <= 5
        fails += not ok
        print(f"  {'OK' if ok else 'MISS':<7} {text}")
        print(f"         -> {fixed}")
        print(f"         head48={head!r}  fact={want_head!r}")

    print(f"\n{'FAILED' if fails else 'passed'}: {fails} mismatch(es)")
    return 1 if fails else 0

if __name__ == "__main__":
    raise SystemExit(main())
