# sim/wearer.py — the validation subject

This is not part of the G1 relay agent. It is the person the agent is measured
against, written by someone who read `DESIGN.md` but not the implementation,
then written *adversarially* to it. That is the whole point: if the agent's
author also wrote the wearer, every result would be a confirmation of the
author's own assumptions, and the most common way an evaluation becomes
worthless is by having no adversary in it at all.

Read the results it produces as "would this survive contact with preferences
nobody designed it around", never as "this wearer is right about people".

---

## What it rewards

**Being right, early, and legible.** Interruptions clear a bar of
`utility / cost`. Three things raise `utility`: latent interest in the item's
subject, real urgency, and a concrete token inside the **first 48 characters**
(two lines of the 5×24 lens). That last one is the wearer's version of the G3
claim: a fact I cannot resolve in one look is not information, it is a reason
to look again and be annoyed. `cost` rises through the day (0.72 at 08:00 to
2.05 at 19:00, with a dip at lunch when interruptions are *expected*), so the
same item is worth interrupting at nine and not at six.

**Restraint that is legible.** A silent queue costs nothing. A queue item I
glance at and find worthless costs me a little patience and **no trust at
all** — that asymmetry is DESIGN's own cost model, and the wearer enforces it.

**Novelty about things I care about.** The first item on a subject is worth
the most; each further sighting *today* is worth 0.80× the last, and each
expansion costs another 0.45×. Measured over the repo's own generator, mean
utility for the 1st…6th item on one topic runs
`1.43, 1.40, 1.18, 1.02, 0.88, 0.78`.

## What it punishes

**Interrupting me about something I already know.** `already_knew` subtracts
utility rather than vetoing it: I still glance at it, I just refuse to be
dragged out of flow for it. Restatements compound (0.30 each). This is the
`redundant >= earned` stop criterion's whole subject, and the wearer fires it
on ~19% of a realistic fortnight.

**Interruptions I did not want, in the short run.** Cost after *n* recent bad
interruptions grows as *n*^1.6:

```
n:        0      1      2      3      4
multiplier 1.00  1.18   1.55   2.04   2.60
increment     —   0.18   0.37   0.50   0.56   (rising, then capped)
```

Three bad interruptions in a row raise the bar from 0.72 to 1.87. A fourth
item that is individually *identical* to the first three is then suppressed —
measured directly in the smoke test, not assumed.

**Unreadable text.** No concrete token in the fovea costs 0.5 utility versus
a legible equivalent, and re-examination of illegible text is ~0% against
~63% for a legible line of the same length. A system that pushes unglanceable
text does not merely fail to earn a second look; it earns irritation.

**Being wrong about what I was told.** `already_knew` is deliberately noisy
(35% recognition miss). Real people do not connect a fourth restatement to
the first, so a redundant push often gets a clean `worth=1`. This makes the
collected `already_knew` labels **optimistic**, which means a milestone
reading `redundant < earned` can look healthy while the product is failing.

## Where it is more adversarial than a real person

- **Trust does not recover on a schedule.** DESIGN proposes one mis-fire
  auto-suspends a source class for 24 h. The wearer has no such reset: trust
  falls per bad interruption and recovers only at 0.03/day. Enough of them and
  it stops responding to *everything* (`abandoned_day` in `report()`). This is
  the harshest thing in the model and it is deliberate — it violates a
  published design decision on purpose.
- **Resentment outlasts the verdict.** An interrupt that is never resolved
  ages into fatigue by itself (`_age_pending`). Silence about the outcome is
  *worse* than a bad outcome, because the cost is paid in uncertainty. A system
  that fires and never follows up is punished harder than one that admits it
  was wrong.
- **Interest drifts under you.** Weights random-walk daily and the top subject
  is *demoted* on day 6 (not deleted — demotion is the harder case for a
  decaying-memory model). Anything that memorised "this person cares about X"
  from day 1 and never re-checks is wrong by day 7.
- **Curiosity floor.** A ~3.5% chance of engaging with something I do not care
  about. People are not threshold functions, so 0.99 precision has to survive
  these rather than be assumed away.
- **Stance bias.** Items marked `stance: confirm` get +15%, `challenge` −15%.
  Slightly irrational, entirely real, and invisible to a relevance score.
- **Cue habituation.** A head-up that has been noise five times stops being
  noticed at all. So `examined` can be a *lower* bound, not only the upper
  bound DESIGN admits to.

## Where it is more forgiving than a real person

- **Queue mistakes are nearly free** (0.05 fragments, no trust loss). Real
  wearers get irritated by a crowded queue, and the model understates that.
  Charging trust for queue noise was my first version; it made the wearer
  abandon on day one for having been shown a spam digest, which measures
  nothing. The cost ceiling exists for the same reason: uncapped, fatigue ×
  evening cost reaches ~14× and the wearer goes **mute**, which would flatter
  any system (nothing gets through, so precision looks perfect).
- **One clean hour restores budget.** Badness decays with a 2.5 h half-life and
  old grudges are dropped after 48 h. Humans hold grudges longer and more
  selectively.
- **Forgetting works properly.** Repeated topics recover ~32% of their
  interest after a day away, and redundancy is counted over a **12-hour**
  window, not forever — a cumulative counter makes every topic permanently
  redundant by day three, which is a bug I had to fix, not a feature.
- **It never lies about its own reading.** The wearer always judges the text it
  is handed. A real person skims, misreads a truncated line, and blames the
  device.

## Design assumptions this model deliberately violates

1. **"Trust recovers on a 24-hour suspension."** It does not. Trust decays
   per event and recovers at 3%/day; abandonment is terminal.
2. **"Interruption cost is roughly linear in annoyance."** It is *n*^1.6, and
   the interaction with time of day is multiplicative, not additive.
3. **"A responder knows what it already knows."** 35% of the time it does not,
   so `already_knew` labels are systematically optimistic.
4. **"The wearer is roughly consistent."** Curiosity, stance bias, time-of-day
   volatility and a day-6 regime shift all move the same item's value by more
   than the effects the system is trying to measure.
5. **"An unanswered push is neutral."** It is worse than a bad one.
6. **"Queue errors and push errors differ only in cost."** They differ in
   *kind*: a redundant push is not noise, it is an insult to time already
   spent, and the design's `false_alarm` count cannot see it because nothing
   false was ever said.

## Honest limits

- The subject list is the repo's own vocabulary (`sim/events.py`), so the
  model binds to this generator. The *dynamics* are the model's own.
- `advance(minutes)` and ISO `ts` both move the clock. A harness that calls
  neither freezes the wearer at 08:00 of day 0, which silently disables
  time-of-day cost, daily trust drift and the day-6 shift.
- Re-examination needs text longer than 48 characters. With the repo's short
  queue lines (`render_queue_line` truncates at 96) the re-read rate is
  legitimately ~0 — see the soak findings, not this file, for why that matters.
- Everything is seeded and deterministic, but a single seed is a single
  person. Conclusions need several seeds and the spread, never one run.

## Reproducing the behaviour checks

`/tmp/w_smoke.py` (throwaway) asserts all six required behaviours: determinism
and report-purity, the superlinear escalator, the time-of-day curve, the
urgent-but-known verdict, monotone diminishing returns averaged over 300
seeds, and readability-driven re-examination.