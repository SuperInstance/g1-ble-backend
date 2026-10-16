# Design brief: G1 wearable relay agent

Repo: /home/proart/projects/glasses. Read `g1_driver.py`, `pipeline.py`,
`session.py`, `memory.py`, `plugins.py`, `main.py` first.

This is phase two. Four defects were found and fixed last round (character
budget, missing meta methods, gesture mis-mapping, dismiss double-count).
Those are done. Do not re-litigate them. Design what comes next.

## Product

Even Realities G1 glasses. Waveguide shows ~5 lines x ~24 chars ≈ 120 chars.
One wearer. He walks around. The system decides what to put on the lens and
compresses it. He can tap a TouchBar to drill into the current item.

## What I believe (challenge it)

This is not a summarization problem, it is an **interruptibility** problem:
does interrupting this person now produce enough expected value to justify the
cost? Silence should be the default state; the drop path is the most
important code, not the relay path.

Prior art that matters:
- Horvitz: Bayesian attention allocation.
- ProMemAssist (arXiv 2507.21378): smart glasses, working-memory modelling,
  threshold decision on utility-vs-interruption-cost, default threshold 0.75.
- Bailey & Konstan: interrupting notifications cause up to +27% task time,
  2x errors. Deferring to breakpoints cuts frustration 20%, reaction time 25%.
- Berkeley glanceability: peripheral displays that need no focal attention have
  near-zero interruption cost; action-requiring ones do not.
- Qi et al. NIPS'18 (E-C Bandit): clicks confound *examination* with
  *relevance*; failing to separate them gives linearly increasing regret.

## New capability I have not used

The G1 emits head-up (`0xF5 0x02`) and head-down (`0xF5 0x03`) events. That is
a **free examination signal** — the latent variable the LTR literature fights
to infer. Head-down means "was not looked at".

This suggests inverting the model: **pull, not push**. Head-up means "show me
the queue." Ambient waveguide content is peripheral and cheap; interruption is
the expensive thing and should be rare and earned.

Also available locally: `ollaya` runs open *decision* models — discriminative,
not generative. `winnow:e4b` exposes `choice`/`score`/`noul` and returns
calibrated probabilities (verified: `difficulty` score 2.1951 with per-option
probability distributions, `needs_tools` noul 0.229, ~22ms warm). It never
generates text; it reads answer-label logits.

## Open questions I need answered

1. **Push vs pull.** Concretely: what does the wearer experience? Is
   head-up-as-queue-right, and what still justifies an actual interruption?
   Design the interaction, including what happens when the queue is long.
2. **The learning substrate.** Volume is ~tens of events/day, one user.
   Implicit learning will not converge in under weeks. Is a nightly review
   with ~30 explicit labels the right primitive? What exactly is the schema?
   Design the `decisions` table and the labels.
3. **The candidate stream.** Everything above assumes events arrive. Design
   what the event source actually is and how candidates are generated, given
   it is a single person.
4. **What runs where.** bleak + local models all day on battery while walking.
   What is the host, what is the power budget, what degrades first?
5. **What is the one thing to build first** such that a week of use produces
   evidence that settles the architecture question.

## Constraints

Single user, local-first, Python, must stay inspectable. No vector DB unless
strongly justified over SQLite. Prefer boring, reversible decisions over
clever ones. Assume the G1 hardware facts are approximately right but
unverified.

## Output

A concrete design, not a survey. Name tables, thresholds, states, and the
first milestone. Where you disagree with me, say so plainly and give the
reasoning. Rank by what de-risks the most per hour of work.