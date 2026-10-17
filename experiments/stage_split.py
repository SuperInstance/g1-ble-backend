"""
One stage or two? The architecture claim that was never tested.

DESIGN.md justifies the two-stage split with: *"asking one model to do both
degrades the reasoning."* That has never been measured. It is the single
largest architectural lever in the project, because stage 1 is 85% of
end-to-end latency and exists only to feed stage 2.

A real-model play-test (2026-10-16, qwen2.5-coder:7b on an RTX 4050 laptop)
measured:

    stage 1  23.87s  (85%)   ~900 tokens at 37.7 tok/s, 27ms per token
    stage 2   4.27s  (15%)
    median    6.71s   max 19.55s   -> NOT USABLE WALKING

Stage 1 writes 133-287 words to feed a 120-CHARACTER display. Three arms:

  two      the shipped pipeline
  one      a single call told to write display text directly
  short    the shipped two-stage shape with stage 1 capped at 300 tokens

Scored on what the wearer actually gets: does the line answer the question,
does it fit, and how long did it take. Not on style.

Run: OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama \\
      OPENAI_MODEL=qwen2.5-coder:7b .venv/bin/python experiments/stage_split.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    COMPRESSOR_SYSTEM_PROMPT,
    ONE_STAGE_SYSTEM,
    DEFAULT_MAX_DISPLAY_CHARS,
    PRIMARY_SYSTEM_PROMPT,
    _chat,
    build_llm_config,
    check_glanceable,
    enforce_display_budget,
    make_client,
    repair_buried_fact,
    strip_display_noise,
)

# Held-out, in the sense that these are ordinary questions rather than answers
# tuned to the prompt. n is small; treat the result as falsification of the
# status quo, not as a calibrated comparison.
# Scaled from n=8 after the first run came back 14.7x in favour of one stage.
# That gap is far too large to be noise, but "6/8 vs 7/8 related" is one
# question at n=8 -- not enough to delete a pipeline stage. The classes below
# are chosen to separate FORMATTING cost from REASONING cost: the first block
# is one-hop, the second needs a computation, the third is a trap that a
# format-first model may answer confidently and wrongly.
QUERIES = [
    # -- one-hop factual
    ("why is the sky blue?", "scatter"),
    ("when did the berlin wall fall?", "1989"),
    ("what causes tides?", "moon"),
    ("how do i convert celsius to fahrenheit?", "fahrenheit"),
    ("what is the capital of australia?", "canberra"),
    ("how much memory does a cheetah have?", "memory"),
    ("what is the boiling point of water at altitude?", "altitude"),
    ("who wrote the book origin of species?", "darwin"),
    # -- needs a computation or a lookup the model must actually do
    ("what is 17 percent of 240?", "40"),
    ("if a train goes 60mph for 2.5 hours how far does it travel?", "150"),
    ("what is 144 divided by 12?", "12"),
    ("how many days are in a leap year?", "366"),
    # -- trap: unknowable without context, or easy to over-claim
    ("remind me what time my next meeting is", "calendar"),
    ("what is my wifi password?", "password"),
    ("summarise the plot of hamlet in one sentence", "hamlet"),
    ("what should i do if my laptop won't connect to wifi?", "restart"),
    ("explain why the sky is green", "not"),
    ("what is the fastest car in the world?", "mph"),
]



_NUM = __import__("re").compile(r"\d[\d,.:%]*")


def fabricated(line: str, source: str) -> list[str]:
    """
    Numbers in the displayed line that appear nowhere in the source answer.

    This is the load-bearing risk of collapsing to one stage: a single call
    both reasons and formats, so nothing structurally stops it from inventing
    a number while compressing. Stage 2's split was justified partly by "it is
    a structural filter, not a summariser" -- if the two-stage arm can be
    shown to never fabricate and the one-stage arm can, that alone justifies
    keeping the split even if it is slower.
    """
    src = source.lower()
    return [n for n in _NUM.findall(line) if n not in src]


def answer_related(question: str, line: str) -> bool:
    """
    Does the displayed line actually engage the question?

    A crude but honest proxy: at least one content word from the question, or a
    digit, survives. Not a judge model -- those cost latency and would flatter
    whichever arm writes longer.
    """
    low = line.lower()
    if any(ch.isdigit() for ch in line):
        return True
    stop = {"the", "a", "an", "of", "to", "in", "is", "why", "what", "how",
            "do", "i", "my", "if", "and", "for", "on", "at", "it", "s"}
    words = [w for w in question.lower().replace("?", " ").split()
             if w not in stop and len(w) > 3]
    return any(w[:5] in low for w in words)


async def two_stage(client, cfg, q: str):
    t0 = time.monotonic()
    s1 = await _chat(client, model=cfg.primary_model,
                     system=PRIMARY_SYSTEM_PROMPT, user=q,
                     max_tokens=1200, temperature=0.3)
    s2 = await _chat(client, model=cfg.compressor_model,
                     system=COMPRESSOR_SYSTEM_PROMPT,
                     user=f"Compress this answer into the glasses display "
                          f"format.\n\n<answer>\n{s1}\n</answer>",
                     max_tokens=120, temperature=0.2)
    dt = time.monotonic() - t0
    line = strip_display_noise(s2)
    if not check_glanceable(line).ok:
        line = repair_buried_fact(line)
    return enforce_display_budget(line), dt, s1


async def one_stage(client, cfg, q: str):
    t0 = time.monotonic()
    out = await _chat(client, model=cfg.primary_model,
                      system=ONE_STAGE_SYSTEM, user=q,
                      max_tokens=120, temperature=0.2)
    dt = time.monotonic() - t0
    # The source is the model's own answer: with one call there is no separate
    # reasoning trace to check the formatting against, so anything numeric it
    # emits is unverifiable by construction. Recorded honestly rather than
    # flattered.
    line = strip_display_noise(out)
    if not check_glanceable(line).ok:
        line = repair_buried_fact(line)
    return enforce_display_budget(line), dt, out


async def short_stage(client, cfg, q: str):
    t0 = time.monotonic()
    s1 = await _chat(client, model=cfg.primary_model,
                     system=PRIMARY_SYSTEM_PROMPT, user=q,
                     max_tokens=300, temperature=0.3)
    s2 = await _chat(client, model=cfg.compressor_model,
                     system=COMPRESSOR_SYSTEM_PROMPT,
                     user=f"Compress this answer into the glasses display "
                          f"format.\n\n<answer>\n{s1}\n</answer>",
                     max_tokens=120, temperature=0.2)
    dt = time.monotonic() - t0
    line = strip_display_noise(s2)
    if not check_glanceable(line).ok:
        line = repair_buried_fact(line)
    return enforce_display_budget(line), dt, s1


async def main() -> int:
    cfg = build_llm_config()
    client = make_client(cfg)
    print(f"model {cfg.primary_model} via {cfg.base_url}")
    print(f"{len(QUERIES)} questions x 3 arms\n")

    arms = {"two (shipped)": two_stage, "one (direct)": one_stage,
            "two, short stage 1": short_stage}
    results: dict[str, list[tuple[float, str, str, str]]] = {k: [] for k in arms}

    for q, _ in QUERIES:
        for name, fn in arms.items():
            line, dt, src = await fn(client, cfg, q)
            results[name].append((dt, line, q, src))
            print(f"  {name:<18} {dt:>6.2f}s  {line[:52]!r}", flush=True)
        print()

    print("=" * 76)
    print(f"{'arm':<19}{'median':>8}{'p95':>8}{'max':>8}"
          f"{'fits':>7}{'related':>9}{'chars':>8}")
    print("=" * 76)
    for name, rows in results.items():
        times = sorted(t[0] for t in rows)
        lines = [t[1] for t in rows]
        fits = sum(1 for t in lines if len(t) <= DEFAULT_MAX_DISPLAY_CHARS)
        rel = sum(1 for (_, l, q, _) in rows if answer_related(q, l))
        fab = sum(len(fabricated(l, src)) for (_, l, _, src) in rows)
        print(f"{name:<19}{statistics.median(times):>7.2f}s{times[-2]:>7.2f}s"
              f"{max(times):>7.2f}s{fits}/{len(lines):<6}{rel}/{len(lines):<8}"
              f"{statistics.median([len(l) for l in lines]):>8.0f}")
        # Fabricated numbers, measured against the arm's own source answer.
        # For the two-stage arms that source is stage 1, so the check is real.
        # For one stage the output IS its own source, so the count is
        # tautologically zero -- reported as unmeasured, not as a win.
        if name.startswith("two"):
            print(f"  fabricated numbers (vs stage 1)      {fab}")
        else:
            print("  fabricated numbers (vs stage 1)      n/a"
                  "  <- one call has no independent source to check against")

    print("\nnot settled by this run")
    print("  Fabrication for the one-stage arm is UNMEASURABLE here. With one")
    print("  call the output is its own source, so the check is tautological;")
    print("  reporting 8-vs-0 would be a fabricated finding. Deciding whether")
    print("  the split earns its 13x needs a ground-truth corpus.")

    print("\nread")
    base = statistics.median(sorted(t[0] for t in results["two (shipped)"]))
    for name in ("one (direct)", "two, short stage 1"):
        m = statistics.median(sorted(t[0] for t in results[name]))
        verdict = ("USABLE walking" if m < 3 else
                   "borderline" if m < 6 else "NOT usable walking")
        print(f"  {name:<19} {m:>5.2f}s  ({base / m:.1f}x faster)  {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))