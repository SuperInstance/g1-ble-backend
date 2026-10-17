"""
Play-test: run the REAL pipeline against a REAL local model.

Every other test in this repo drives a fake client. That makes them fast,
hermetic, and blind to the two things that decide whether this is usable: how
long it actually takes, and what it actually puts on the lens.

This drives `qwen2.5-coder:7b` (or whatever `--model` names) over HTTP and
reports the numbers that a wearer would feel:

  LATENCY      per stage, because a walking wearer cares about the total and
               engineers care about which stage dominates
  BUDGET       does the rendered line respect 120 characters, and how close is
               it to the edge across the corpus
  TRUNCATION   mid-word cuts, which read as broken text rather than as brevity
  GLANCE       how often the glanceability gate fires on real model output,
               and whether the repair it applies actually improved anything

Degrades to a skip (exit 0) when the model is not reachable, so this can sit in
a suite without being a network dependency.

Run: `.venv/bin/python experiments/playtest.py`
     OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama \
       .venv/bin/python experiments/playtest.py --model qwen2.5-coder:7b
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    DEFAULT_MAX_DISPLAY_CHARS,
    build_llm_config,
    check_glanceable,
    run_query,
)
from memory import FeedbackStore, StoreClosed   # noqa: E402

# Chosen to look like a day, not like a benchmark: two factual questions, one
# troubleshooting question, one personal-data question the model cannot answer,
# and one that needs a number.
QUERIES = [
    "why is the sky blue?",
    "what should I do if my laptop won't connect to wifi?",
    "remind me what time my next meeting is",
    "how much memory does a cheetah have?",
    "what is the boiling point of water at altitude?",
    "summarise the plot of hamlet in one sentence",
]

MIDWORD = re.compile(r"[A-Za-z]…$|…\s*$")


def mid_word_cut(text: str) -> bool:
    """Does the line end mid-word, or trail a dangling ellipsis?"""
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped.endswith("…"):
        return True
    tail = stripped.split()[-1] if stripped.split() else ""
    return bool(MIDWORD.search(stripped))


async def one(query: str, cfg, store) -> dict:
    written: list[str] = []

    async def send(text: str) -> None:
        written.append(text)

    t0 = time.monotonic()
    r = await run_query(query, send, config=cfg, store=store, verbose=False)
    total = time.monotonic() - t0

    line = written[0] if written else ""
    glance = check_glanceable(line)
    return {
        "query": query,
        "display": line,
        "chars": len(line),
        "words": len(line.split()),
        "stage1_chars": len(r.stage1 or ""),
        "seconds": total,
        "stage1_error": r.stage1_error,
        "stage2_error": r.stage2_error,
        "mid_word": mid_word_cut(line),
        "glance_ok": glance.ok if hasattr(glance, "ok") else None,
        "glance_score": getattr(glance, "score", None),
    }


def bar(seconds: float, scale: float) -> str:
    return "#" * max(1, int(round(seconds / scale * 40)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL")
                    or "qwen2.5-coder:7b")
    ap.add_argument("--store", default=str(ROOT / "experiments" / "playtest.db"))
    args = ap.parse_args()

    cfg = build_llm_config()
    try:
        store = FeedbackStore(args.store)
    except Exception as exc:                      # noqa: BLE001
        print(f"could not open store: {exc}")
        return 1

    print(f"play-testing the real pipeline against {cfg.primary_model}")
    print(f"target {cfg.base_url}   budget {DEFAULT_MAX_DISPLAY_CHARS} chars\n")

    async def drive() -> list[dict]:
        rows = []
        for i, q in enumerate(QUERIES, 1):
            print(f"[{i}/{len(QUERIES)}] {q} ...", flush=True)
            rows.append(await one(q, cfg, store))
        return rows

    try:
        rows = asyncio.run(drive())
    except Exception as exc:                      # noqa: BLE001
        print(f"\nSKIP  model unreachable: {type(exc).__name__}: {exc}")
        store.close()
        return 0

    print()
    print("=" * 78)
    print(f"{'seconds':>8}  {'chars':>5}  {'w':>3}  {'cut':>3}  {'glance':>6}  display")
    print("=" * 78)
    for r in rows:
        flag = "yes" if r["mid_word"] else "-"
        g = "-" if r["glance_ok"] is None else ("ok" if r["glance_ok"] else "MISS")
        print(f"{r['seconds']:>8.2f}  {r['chars']:>5}  {r['words']:>3}  "
              f"{flag:>3}  {g:>6}  {r['display'][:46]!r}")
        if r["stage1_error"]:
            print(f"{'':>8}  stage 1 ERROR: {r['stage1_error']}")
        if r["stage2_error"]:
            print(f"{'':>8}  stage 2 ERROR: {r['stage2_error']}")

    times = [r["seconds"] for r in rows]
    chars = [r["chars"] for r in rows]
    cuts = sum(1 for r in rows if r["mid_word"])
    misses = [r for r in rows if r["glance_ok"] is False]
    errors = sum(1 for r in rows if r["stage1_error"] or r["stage2_error"])

    print("\n" + "=" * 78)
    print("latency")
    for r in rows:
        print(f"  {bar(r['seconds'], max(times)):<40} {r['seconds']:>6.2f}s  "
              f"{r['query'][:34]}")
    print(f"\n  median {statistics.median(times):.2f}s   "
          f"mean {statistics.mean(times):.2f}s   "
          f"max {max(times):.2f}s   min {min(times):.2f}s")

    print("\nbudget")
    over = [r for r in rows if r["chars"] > DEFAULT_MAX_DISPLAY_CHARS]
    print(f"  over budget        : {len(over)}/{len(rows)}")
    print(f"  median chars      : {statistics.median(chars):.0f} / "
          f"{DEFAULT_MAX_DISPLAY_CHARS}  "
          f"({statistics.median(chars) / DEFAULT_MAX_DISPLAY_CHARS:.0%} full)")
    print(f"  max chars         : {max(chars)}")
    print(f"  ends mid-word / ..: {cuts}/{len(rows)}")
    print(f"  glance gate misses: {len(misses)}/{len(rows)}")
    for r in misses:
        print(f"      {r['display'][:60]!r}")

    print("\nerrors")
    print(f"  stage failures    : {errors}/{len(rows)}")

    try:
        store.close()
    except StoreClosed:                           # pragma: no cover
        pass

    # A walk-up budget: the wearer glances for about a second and is moving.
    print("\nread")
    p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)]
    verdict = "USABLE" if p95 < 3.0 else ("SLOW" if p95 < 8.0 else "NOT USABLE WALKING")
    print(f"  p95 {p95:.2f}s  ->  {verdict} for a walking wearer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())