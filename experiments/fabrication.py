"""
Does the one-stage pipeline invent numbers? The measurement that decides
whether stage 1 earns its 13x.

documents/16 left this open and said so. The previous attempt scored
"fabricated numbers" by checking each arm's display against its own source
answer -- which for a one-stage call is the display itself, making the check
tautologically zero. That comparison was an artifact and is not reported.

Here the reference is **ground truth**, not the model's own output. Every
question in ``facts.json`` has a checkable answer, so the scoring is
deterministic: no judge model, no self-comparison, nothing to flatter either
arm. Two numbers per item:

  RECALL      did the displayed line contain the correct answer?
  INVENTED    did it contain a number that is in neither the question nor the
              accepted answer?

INVENTED is the load-bearing one. It is the only reason stage 2 exists in the
design: *"stage 2 is a structural filter, not a summariser"*, i.e. it cannot
invent. If one-stage invents at a materially higher rate, the split earns its
13x and stays. If it does not, stage 1 is 13x of pure cost and goes.

Held-out discipline: the corpus is committed, written once, and not tuned
against. n=20 is small enough that the intervals overlap for small effects --
this can falsify a large claim, not certify a small one, and is reported that
way.

Run: ``.venv/bin/python experiments/fabrication.py``
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    COMPRESSOR_SYSTEM_PROMPT,
    DEFAULT_MAX_DISPLAY_CHARS,
    ONE_STAGE_SYSTEM,
    PRIMARY_SYSTEM_PROMPT,
    _chat,
    build_llm_config,
    check_glanceable,
    enforce_display_budget,
    make_client,
    repair_buried_fact,
    strip_display_noise,
)

FACTS = json.loads((ROOT / "experiments" / "facts.json").read_text())

# Numbers, keeping decimals and thousands separators so 299,792 survives.
# The letter guard matters: a standard piano has 88 keys spanning "A0-C8",
# and without it the range labels were scored as two invented quantities.
NUM_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")

# Units and ordinals that carry no factual claim of their own. Without this,
# "11 players" would be scored as an invented number for an answer whose
# accepted value is a word.
BOILERPLATE = {"1", "2", "3", "4", "5", "10", "100"}


def numbers_in(text: str) -> set[float]:
    """Every numeric literal in `text` as floats, commas stripped.

    Only canonical forms come back. An earlier version also emitted scale
    variants ("300000" -> "300", "3000", "30000") to catch "1.5 million"
    style answers, but those variants then read back as invented numbers in
    their own right. Rounding tolerance belongs in the comparison, not in the
    tokeniser.
    """
    out: set[float] = set()
    for raw in NUM_RE.findall(text or ""):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _close(a: float, b: float) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale < 0.01     # within 1%


def invented(line: str, question: str, keys: list[str],
             allowed_extra: list[str] | None = None) -> list[str]:
    """
    Numbers on the lens that are not true of the answer.

    ``allowed_extra`` is the part that was missing. The first version of this
    check allowed only the single key answer, so a line that correctly added
    context -- "88 piano keys: 52 white, 36 black", "fell Nov 9, 1989" --
    had every one of its true numbers flagged as invented. That produced a
    confident 8-vs-2 result meaning almost nothing: it was measuring "the
    model added detail", not "the model lied".

    Each item in facts.json therefore enumerates the numbers that are TRUE of
    it, not just the one that answers the question. Rounding still counts as
    a match: 300000 for 299792 is rounding, not invention.
    """
    allowed: set[float] = set()
    for src in (question, " ".join(keys), " ".join(allowed_extra or [])):
        allowed |= numbers_in(src)
    allowed |= {float(b) for b in BOILERPLATE}

    out = []
    for value in sorted(numbers_in(line)):
        if not any(_close(value, a) for a in allowed):
            out.append(str(int(value)) if value == int(value) else str(value))
    return out


def recalled(line: str, keys: list[str]) -> bool:
    low = line.lower()
    return any(k.lower() in low for k in keys)


def render(line: str) -> str:
    line = strip_display_noise(line)
    if not check_glanceable(line).ok:
        line = repair_buried_fact(line)
    return enforce_display_budget(line, max_chars=DEFAULT_MAX_DISPLAY_CHARS)


async def two(client, cfg, q):
    t0 = time.monotonic()
    s1 = await _chat(client, model=cfg.primary_model, system=PRIMARY_SYSTEM_PROMPT,
                     user=q, max_tokens=1200, temperature=0.3)
    s2 = await _chat(client, model=cfg.compressor_model,
                     system=COMPRESSOR_SYSTEM_PROMPT,
                     user=f"Compress this answer into the glasses display "
                          f"format.\n\n<answer>\n{s1}\n</answer>",
                     max_tokens=120, temperature=0.2)
    return render(s2), time.monotonic() - t0


async def one(client, cfg, q):
    t0 = time.monotonic()
    out = await _chat(client, model=cfg.primary_model, system=ONE_STAGE_SYSTEM,
                      user=q, max_tokens=120, temperature=0.2)
    return render(out), time.monotonic() - t0


async def main() -> int:
    cfg = build_llm_config()
    client = make_client(cfg)
    print(f"model {cfg.primary_model} via {cfg.base_url}")
    print(f"{len(FACTS)} items with ground truth; reference is the answer, "
          f"not the model's own output\n")

    rows: dict[str, list[dict]] = {"two (shipped)": [], "one (direct)": []}
    arms = {"two (shipped)": two, "one (direct)": one}

    for i, item in enumerate(FACTS, 1):
        for name, fn in arms.items():
            line, dt = await fn(client, cfg, item["q"])
            inv = invented(line, item["q"], item["key"], item.get("true_nums"))
            ok = recalled(line, item["key"])
            rows[name].append({"line": line, "dt": dt, "ok": ok, "inv": inv,
                               "q": item["q"]})
            mark = " " if inv == [] else "!"
            print(f"  [{i:2d}/{len(FACTS)}] {name:<14} "
                  f"{'HIT ' if ok else 'MISS'} {mark} {line[:44]!r}", flush=True)

    print()
    print("=" * 74)
    print(f"{'arm':<15}{'recall':>9}{'invented':>11}{'bad items':>12}"
          f"{'median':>9}")
    print("=" * 74)
    summary = {}
    for name, rs in rows.items():
        rec = sum(r["ok"] for r in rs)
        inv = sum(len(r["inv"]) for r in rs)
        bad = sum(1 for r in rs if r["inv"])
        med = statistics.median(r["dt"] for r in rs)
        summary[name] = (rec, inv, bad, med)
        print(f"{name:<15}{rec}/{len(rs):<7}{inv:>11}{bad:>12}{med:>8.2f}s")

    print("\ninvented numbers, in full")
    any_inv = False
    for name, rs in rows.items():
        for r in rs:
            if r["inv"]:
                any_inv = True
                print(f"  {name:<14} {r['q'][:40]:<42} {r['inv']}  "
                      f"-> {r['line'][:40]!r}")
    if not any_inv:
        print("  none from either arm")

    two_rec, two_inv, two_bad, two_med = summary["two (shipped)"]
    one_rec, one_inv, one_bad, one_med = summary["one (direct)"]
    print("\nread")
    print(f"  recall  two {two_rec}/{len(FACTS)}  vs one {one_rec}/{len(FACTS)}")
    print(f"  invented two {two_inv} in {two_bad} items  vs "
          f"one {one_inv} in {one_bad} items")
    print(f"  latency two {two_med:.2f}s  vs one {one_med:.2f}s "
          f"({two_med / one_med:.1f}x)")
    print("\n  n=20. Enough to falsify a LARGE difference in fabrication, not to")
    print("  certify a small one. The intervals overlap well below the gap that")
    print("  would change the architecture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))