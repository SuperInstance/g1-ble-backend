"""
Does the interest block change anything? The A/B that has been promised twice.

`.context/llm.md` has listed "an A/B with preferences='' shows the same
expand/dismiss rates" as an open falsification since the feature was built, and
documents/16 says the injection path is "unproven". It stayed unrun because
the flag did not exist; then because the default architecture moved and the
stage-2 prompt stopped being sent. Both reasons are now gone.

The honest framing, because this is easy to fake:

**With a 120-character display, personalisation cannot make an answer more
TRUE.** The fact is already correct or it is not, and injecting a topic cannot
change arithmetic. What it can change is WHICH correct fact is chosen when the
question has more than one good answer. So the metric is not "is the answer
better" -- it is:

  1. DIFFERENCE   does the block change the output at all? A block that never
                  changes a byte is decoration, and should be removed.
  2. TIE-BREAK    on questions with two legitimately answerable facts, does the
                  interest-matching fact lead more often with the block on?
  3. COST         does it push the line over budget or shorten it usefully?

Paired design: identical queries, identical temperature, both arms, order
alternated to cancel any warm-up drift. Reported as counts, because at this n
no difference is statistically meaningful and saying so is part of the result.

Run:
  OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama \\
  OPENAI_MODEL=qwen2.5-coder:7b .venv/bin/python experiments/personalisation_ab.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import FeedbackStore                       # noqa: E402
from pipeline import (                                 # noqa: E402
    DEFAULT_MAX_DISPLAY_CHARS,
    build_llm_config,
    build_one_stage_system,
    check_glanceable,
    make_client,
    repair_buried_fact,
    strip_display_noise,
    _chat,
)

# TWO corpora, because the first run of this experiment accidentally found the
# real answer and it needs separating from the effect measurement.
#
# REALISTIC: ordinary questions. These measure HOW OFTEN the salience gate fires
# in actual use -- the base rate, which is a property of the shipped design and
# not of any effect.
REALISTIC = [
    "what happened with the build?",
    "tell me about the migration",
    "how did the interview go?",
    "anything new on the deploy?",
    "did the payment go through?",
    "what is the status of the wifi issue?",
    "should I worry about the invoice?",
    "did the doctor confirm the appointment?",
]

# Topic-bearing: these DO name an interest, so the gate fires. They measure the
# CONDITIONAL effect -- what happens when the block is actually sent. Without
# these the first run measures nothing at all.
TIE_CASES = [
    # (question, the fact that is "main", the interest-matching fact)
    ("did the build finish, or do we need to rollback?",
     "build finished", "rollback"),
    ("is the migration done, or should we rollback?",
     "migration finished", "rollback"),
    ("what is the priority on ticket 4471?",
     "ticket updated", "priority"),
    ("how did the salary offer land?",
     "interview finished", "salary"),
    ("should we rollback the deploy?",
     "deploy finished", "rollback"),
    ("is the contract renewal processed?",
     "contract signed", "renewal"),
    ("is there a firmware update pending?",
     "issue resolved", "firmware"),
    ("has the refund been issued?",
     "payment cleared", "refund"),
]

INTERESTS = ("rollback", "priority", "salary", "renewal", "firmware", "refund")


def seed(store: FeedbackStore, interests: tuple[str, ...]) -> None:
    """Give the store real evidence: each interest starred on 2+ distinct cards."""
    for topic in interests:
        for n in range(2):
            cid = store.add_card(
                query=f"{topic} note {n}", stage1="x" * 300,
                summary=f"About {topic} number {n}", keywords=[topic],
            ).id
            store.add_event(cid, "star")


def normalise(text: str) -> str:
    line = strip_display_noise(text)
    if not check_glanceable(line).ok:
        line = repair_buried_fact(line)
    return line[:DEFAULT_MAX_DISPLAY_CHARS]


async def one(client, model: str, q: str, topics) -> tuple[str, float]:
    t0 = time.monotonic()
    out = await _chat(client, model=model, system=build_one_stage_system(topics),
                      user=q, max_tokens=120, temperature=0.2)
    return normalise(out), time.monotonic() - t0


def mentions(line: str, phrase: str) -> bool:
    return phrase.lower() in line.lower()


async def main() -> int:
    cfg = build_llm_config()
    client = make_client(cfg)
    model = cfg.primary_model or ""
    print(f"model {model} via {cfg.base_url}")
    print(f"{len(TIE_CASES)} tie cases, {len(INTERESTS)} interests, paired arms\n")

    with tempfile.TemporaryDirectory() as tmp:
        store = FeedbackStore(Path(tmp) / "ab.db")
        seed(store, INTERESTS)
        ctx = store.wearer_context()
        print(f"  store seeded: interests = {ctx.interests}")
        print(f"  gate: query must contain a topic; `mentions()` is a case-\n"
              f"        insensitive substring test, deliberately strict\n")

        # -- base rate: how often does the gate fire on ordinary questions? --
        fired = sum(1 for q in REALISTIC if ctx.mentions(q))
        print(f"\n  BASE RATE on {len(REALISTIC)} ordinary questions: "
              f"{fired}/{len(REALISTIC)} passed the salience gate")
        if fired == 0:
            print("  The gate requires the QUESTION to literally contain an")
            print("  interest word. Natural phrasing does not do that, so on")
            print("  ordinary traffic the block is not sent at all. This is a")
            print("  property of the design, not of this corpus.")
        print()

        rows = []
        for i, (q, main, interest) in enumerate(TIE_CASES):
            gated_on = ctx.mentions(q)
            # Alternate order so a warm-up drift cannot favour one arm.
            order = [True, False] if i % 2 == 0 else [False, True]
            got = {}
            for flag in order:
                topics = ctx.interests if flag else None
                got[flag] = await one(client, model, q, topics)

            on_line, on_dt = got[True]
            off_line, off_dt = got[False]
            rows.append({
                "q": q, "gated": gated_on,
                "changed": on_line != off_line,
                "on_interest": mentions(on_line, interest),
                "off_interest": mentions(off_line, interest),
                "on_len": len(on_line), "off_len": len(off_line),
                "on_dt": on_dt, "off_dt": off_dt,
                "on_line": on_line, "off_line": off_line,
            })
            mark = "GATED OFF" if not gated_on else ""
            print(f"[{i+1}/{len(TIE_CASES)}] {q[:38]:<40} "
                  f"{'CHANGED' if rows[-1]['changed'] else 'identical'} "
                  f"{mark}")
            if gated_on:
                print(f"      on : {on_line[:52]!r}")
                print(f"      off: {off_line[:52]!r}")

        gated = [r for r in rows if r["gated"]]
        print("\n" + "=" * 74)
        print("CONDITIONAL EFFECT (queries that name an interest)")
        print("=" * 74)
        print(f"{'metric':<34}{'value':>10}{'n':>8}")
        print("=" * 74)
        changed = sum(r["changed"] for r in gated)
        on_hit = sum(r["on_interest"] for r in gated)
        off_hit = sum(r["off_interest"] for r in gated)
        over = sum(r["on_len"] > DEFAULT_MAX_DISPLAY_CHARS for r in gated)

        print(f"{'queries passing the interest gate':<34}{len(gated):>10}"
              f"{len(rows):>8}")
        print(f"{'outputs CHANGED by the block':<34}{changed:>10}{len(gated):>8}")
        print(f"{'interest fact present WITH block':<34}{on_hit:>10}{len(gated):>8}")
        print(f"{'interest fact present WITHOUT block':<34}{off_hit:>10}{len(gated):>8}")
        print(f"{'over 120 chars with block':<34}{over:>10}{len(gated):>8}")
        if gated:
            print(f"{'median chars on / off':<34}"
                  f"{statistics.median(r['on_len'] for r in gated):.0f}/"
                  f"{statistics.median(r['off_len'] for r in gated):.0f}"
                  f"{'':>8}")
            print(f"{'median latency on / off':<34}"
                  f"{statistics.median(r['on_dt'] for r in gated):.2f}s/"
                  f"{statistics.median(r['off_dt'] for r in gated):.2f}s"
                  f"{'':>8}")

        print("\nread")
        if not gated:
            print("  No query passed the salience gate, so the block was never")
            print("  sent and this run measures nothing. That is itself the")
            print("  result: the gate is strict enough to be inert.")
            return 0
        if changed == 0:
            print("  THE BLOCK IS DECORATION. It never changed a single output.")
            print("  Rule 9 (never go looking for the subject) is doing its job")
            print("  so thoroughly that the block cannot act -- and the honest")
            print("  response is to remove it, not to relax the rule.")
        elif on_hit <= off_hit:
            print(f"  The block changed {changed} outputs but did NOT lead with the")
            print(f"  interest fact more often ({on_hit} vs {off_hit}). At n={len(gated)}")
            print("  that is not a win, and it is not a loss either -- it is an")
            print("  absence of evidence, which is what this has been for weeks.")
        else:
            print(f"  The block led with the interest fact {on_hit} times vs {off_hit}.")
            print(f"  n={len(gated)} is far too small to conclude anything from a gap")
            print("  of this size. Treat it as a reason to keep measuring, not as")
            print("  a result.")
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))