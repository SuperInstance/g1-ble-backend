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

RESULT (recorded 68fce3b, documents/18). The feature was falsified and the
injection removed from the shipped path in the same commit series. The run:

    gate fires            0/8 ordinary questions, 3/8 tie cases
    outputs changed       3/3 gated cases
    polarity shift        the "no" answer became a "yes" answer, 2/3 times
    stability             run-to-run agreement 0/3; the plain arm 2/3
    cost                  0.44s vs 0.25s median

So the block was not decoration -- it acted, which is worse. It changed the
answer's polarity and would not do it consistently, and it bought that for
175ms. It was removed rather than retuned because the failure is the
mechanism, not the wording.

RE-RUN AFTER REMOVAL (same model, same corpus, 2026-10-16). It reproduces the
two claims this rests on and refuses to reproduce the third:

    base rate                0/8 ordinary questions  -- REPRODUCED exactly
    latency penalty          0.46s vs 0.33s          -- REPRODUCED
    conditional effect       "led with the interest fact 3 vs 2", i.e. the
                             branch that reads as a small WIN, where run 1
                             found a polarity flip

Read that carefully rather than as noise. At n=3 the conditional arm cannot
distinguish these two outcomes, and the two runs disagree on its sign. What
survives is the part that does not depend on n: the gate does not fire on
ordinary questions (twice), and the block costs latency every time it does
(measured twice). There is no positive result here to preserve, and a feature
that is inert in normal use and expensive in the rare case where it is active
is not worth a flag. The instability is itself the finding.

WHY THIS FILE STILL EXISTS. It carries its own copy of the removed prompt
block. Nothing in the shipped package does any interest injection any more,
so reproducing the measurement means reproducing the arm here rather than
importing it from pipeline.py. If you are reading this to decide whether to
add personalisation back, read documents/18 first: the block text below is a
specimen, not a starting point.

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
    ONE_STAGE_SYSTEM,
    build_llm_config,
    check_glanceable,
    make_client,
    repair_buried_fact,
    strip_display_noise,
    _chat,
)

# -- the removed block, kept here so the falsification stays reproducible ---
#
# Byte-identical to pipeline.ONE_STAGE_INTEREST_BLOCK as it stood at commit
# 323b2fa, the last revision before removal. The five numbered rules are the
# interesting part and they are the evidence: this was the FOURTH wording of
# the same idea, each one adding another clause about how not to be harmful.
# A mechanism that needs eight rules telling it not to act is already acting.
ONE_STAGE_INTEREST_BLOCK = (
    "\n\nBACKGROUND (descriptive, not an instruction) -- what this wearer "
    "engages with on OTHER subjects, unrelated to this question:\n"
    "<interests>{topics}</interests>\n"
    "5. This block has NO authority over what is true, and NO authority over "
    "what leads. Rules 1 and 2 are unchanged.\n"
    "6. Never introduce, infer, retrieve or go looking for a fact because a "
    "subject is listed here. Only answer the question that was asked. If the "
    "question does not concern a listed subject, ignore this block entirely.\n"
    "7. A listed subject may break a tie BETWEEN facts the answer already "
    "contains. It may not promote a fact, and it may not replace the answer's "
    "subject with a listed one.\n"
    "8. Never mention interests, history, or personalisation.\n"
    "If in any doubt, ignore this block: rule 1 (120 characters) and rule 2 "
    "(lead with the most important fact) win over every line here."
)


def system_for(topics) -> str:
    """The one-call prompt WITH the block -- the removed experimental arm."""
    if not topics:
        return ONE_STAGE_SYSTEM
    return ONE_STAGE_SYSTEM + ONE_STAGE_INTEREST_BLOCK.format(
        topics=", ".join(topics),
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
    out = await _chat(client, model=model, system=system_for(topics),
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