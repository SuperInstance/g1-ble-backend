# Panel: learning substrate

Reviewer angle: "be the person who is skeptical of machine learning here."

Every DDL, view and query referenced below was executed against SQLite 3.46.1.
The numbers are computed output, not illustration. Nothing in the repo was
modified; validation used scratch databases in `/tmp`.

Schema: [`08-schema.sql`](08-schema.sql).

## 0. What is actually wrong, measured

Three defects, each verified against the code:

**(a) The same tap is counted twice under contradictory readings.**
`EVENT_WEIGHTS["expand"] = 0.5` is credited to *topic interest* in
`topic_weights()`, and **the same event** is credited again with `+0.5` in
`needs_detail()` under the opposite reading — "this topic should get *more*
words per summary." Four taps on one topic:

```
topic_weights after 4 expands : {'kernel': 3.0}   <- saturated at MAX_WEIGHT
needs_detail  after 4 expands : {'kernel': 2.0}   <- passed its gate after 2
```

The clamp means taps 5, 6, 7 contribute nothing. The signal is destroyed by the
clamp, not extracted by it.

**(b) Silence is structurally incapable of being negative.** `topic_weights()`
iterates `events` only, so a card with zero reactions contributes nothing — then
un-reacted keywords are seeded at exactly `1.0` and left there forever. There
is no way to represent "we showed this and you did not care", which is the only
signal that teaches suppression.

**(c) Provenance in the database is already false.** `confirm()` writes
`detail="touchbar/triple-tap"`, but the byte bound to `confirm` is `0x17`,
"TouchBar held". Any provenance filter built on `events.detail` filters on
fiction.

Also: `star` has a weight and a handler but no bound byte — dead code. And
`extract_keywords` draws features from the ~300-word answer while the wearer
reacted to 120 characters.

## 1. Signal decomposition: two axes, not one

Two reactions carry two bits at once.

| Observation | Relevance R | Quality Q | Confounded? |
|---|---|---|---|
| **expand** (tap) | **+1** — engaged, topic matters | **−1** — 120 chars insufficient | **Yes.** Positive R, *negative* Q. |
| **confirm** (held) | +1 | +1 | No — the readings agree. |
| **dismiss** (root) | −1 if examined, **?** if not | **?** — cannot be separated from R | **Worse than expand.** |
| **head-up then dismiss** | **−1** — examined and refused | ≈0 | **Clean.** |
| **no reaction** | 0 / censored | 0 / censored | Currently discarded. |

`expand` is the case that breaks a scalar model, and it is worth being precise
about why:

- For **relevance**, expand is *strong positive* evidence — he chose to spend
  8 seconds and an LLM call on this.
- For **quality**, expand is *negative* evidence — the summary failed.

A single scalar must average them, so **quality is recorded as interest.** The
model has no representation for "good topic, bad 120 characters" — the most
actionable state for this product, because it has a fix: change the
compression, keep the topic. The current design cannot distinguish "never relay
stripe API news" from "relay it but spend words differently," and those demand
opposite actions.

**Rule: split any reaction whose two readings disagree in sign. Leave concordant
reactions alone.** `confirm` is the one case where a scalar is safe.

## 2. The volume problem — explicit labels, not inference

Not because implicit feedback is wrong in principle, but because at this volume
it **cannot converge before the decay constant erases it**. From the code's own
parameters:

```
30 cards/day x 12 keywords/card over ~60 distinct keywords
  = 6.0 observations per keyword per day

  rate ±10% at p=0.5 : 97 obs  -> 16.2 days per keyword
  rate ±10% at p=0.2 : 62 obs  -> 10.3 days per keyword
  rate  ±5% at p=0.2 : 246 obs -> 41.0 days per keyword

HALF_LIFE_DAYS = 7.0
```

**The decay horizon is shorter than the time to learn a single feature.** After
10 days you have a ±10% estimate of one keyword, of which the 7-day half-life
has already discarded half the evidence. The system is fitting a moving target
with a smoothing kernel shorter than its convergence time. This is not slow
convergence; it is non-convergence.

The refiner makes it worse: at 30 events/day the gate opens on **day 0.7** and
re-runs every **8 hours**, rewriting a free-text prompt from a 40-card sliding
window with no held-out set. Nothing in the system can say "I don't know yet."

Explicit labels have no such floor: 30/day x 30 days = **900 unambiguous
ground truths per month.**

**The review (`--review`)**, one card per screen:

```
  [14/30]  21:04:38   examined ✗  outcome: timeout

  » Stripe is deprecating the Charges API on 2026-11-01.

    was this worth interrupting you?   y / n  s=skip  q=quit  ?=why
```

`?` dumps the exact per-feature log-odds audit from the query in
`08-schema.sql` — the same query the system runs, not a paraphrase.

Fast because there is **no LLM call in the loop** — it runs entirely off the
database. Single keypress without Enter, three items pre-rendered, `j`/`k` to
bulk-apply a run of obviously-bad items. Median well under 60 s/day.

**The stratification is not optional.** Stratum A is uniform over everything
observable (the only stratum where a false-positive rate is estimable);
Stratum B deliberately over-samples never-examined items. In testing, **21 of
30 queued items were never examined** — invisible mass that a
reactions-only review would miss entirely. Reviewing only items you reacted to
makes every sample item *selected*, and the intercept becomes unidentifiable.

## 3. The examination problem

The glasses emit head-up / head-down (`0xF5 0x02` / `0x03`). They are decoded
today and **discarded** by the `action is None` early-return. The hook goes
*before* that return:

```python
if len(data) < 2 or data[0] != 0xF5:
    return
self.store.note_glance(data[1])      # always recorded, O(1), off the hot path
action = self.touch_actions.get(data[1])
if action is None:
    return
```

What it buys:

| Pattern | Meaning | Counts as |
|---|---|---|
| head-up within 8 s of impression | examined | `examined = 1` |
| **2+ head-ups on the same card** | read it, put it down, came back — **the 120 chars didn't work first time** | `Q_neg += 1` |
| `0x06` removed / `0x07` case / `0x1E`–`0x1F` dashboard | **not observable** | excluded from calibration |
| head-up then root dismiss | examined and refused | `R_neg += 1` — clean |
| no head-up then root dismiss | ambiguous | *censored* |
| no reaction, observable | never examined | `R_neg += 0.25` |

The re-examination case is the cheapest quality signal in the system and costs
the wearer zero attention: **head-up → head-down → head-up** means the text was
not readable at a glance, which is a compression defect, not a topic defect.

Cross-tab over 630 decisions, only visible once examination is logged:

```
  labelled, examined          -> yes=40   no=171   (211)
  labelled, never examined    -> yes=64   no=302   (366)
```

Two facts fall out: **~40% of confirms were never examined** (hold-the-TouchBar
is not reliably preceded by a head-up — a real finding about the device), and
**64 never-examined items were worth interrupting**, which the current model
holds at neutral forever.

> **Caveat, carried forward.** The design review panel later argued that
> `0x02`/`0x03` are the firmware's *dashboard* gesture and that head-down does
> not mean "did not read pushed content" — see
> [`05-claude-design.md`](05-claude-design.md) point 2. Treat `examined` as
> unproven until real wear data settles it.

## 4. Scoring — auditable by construction

Beta(1,1) prior; weight is the posterior log-odds ratio. The score is a dot
product **normalised by the feature vector's L2 norm**, which gives two useful
properties:

- A one-feature decision reads as exactly that feature's log-odds (verified:
  `1.6094 == ln(10/2)`). No hidden scaling to explain.
- A candidate carrying twelve weak features cannot out-shout one carrying a
  single strong, well-evidenced feature.

Real audit output:

```
=== why did decision #1 relay? ===
  topic:kernel   x1.0  w=+1.609  [+1.407,+1.812]  p=0.83 n=10   -> +1.609
  chars:118      x1.0  w=-0.357  [-0.584,-0.129]  p=0.41 n=15   -> -0.357
  RELAY  score=0.8858 vs threshold=0.5000
```

The CI column lets the system admit uncertainty. `chars:118` at n=15 carries
`[-0.584, -0.129]` — it does not know whether 118 characters is good. That
uncertainty is currently invisible and would be treated as a fact.

**Why freeze the feature vector:** the posterior moves. Storing only
`feature_key` means you cannot reconstruct why decision #412 fired on Tuesday.
Freezing makes the audit permanent rather than approximate.

## 5. Calibration from the wearer's own data

**The ordering is the whole argument.** You cannot calibrate a threshold
without an independent label, and stated confidence from a walking human is not
one — the literature on stated vs. revealed preference is unambiguous that it is
poorly calibrated. The review label is what makes calibration meaningful.

```
python main.py --calibrate --days 14 --fp-cost 5 [--apply]
```

`--fp-cost 5` encodes "a wasted interruption costs five times a missed one."

Verified output, held-out days 15–21, base rate 0.173, score AUC 0.742:

```
   tau    n   tp   fp   fn  prec    rec   cost
  2.70  210    3    1   31  0.750  0.088   36.0
  argmin tau = 2.70
```

At a 17% base rate and 5:1 cost ratio the optimum is very conservative: 4
interrupts per 210 candidates, 75% of them wanted. Unintuitive enough that it
should be earned from data rather than assumed.

**Report the plateau, not the argmin.** Every tau in `[2.70, 3.40]` is within
10% of optimal — 15 grid points. Publishing `2.70` is false precision.
Bootstrap over **days**, not rows: drift is what you are unsure about, so the
day is the resampling unit. Verified: `p10=2.50, median=2.70, p90=2.70`.

**The guard that prevents a meaningless answer.** If the argmin lands on the
grid **edge**, the correct output is "your feature set has no signal" — not
"raise the threshold." Verified: with a score generated independently of the
label, the sweep returns the grid maximum at every cost ratio.

This matters because **today a feature set with no signal still produces
confident-looking weights and a confident-looking `preferences()` string**,
because there is no held-out set anywhere in the system to say "we have
nothing."

**Honesty about sample size:** base rate ±5pp needs n ≥ 220 labels = 7 days at
30/day. **The system can be honestly calibrated around day 8, not day 1.**
Before that `--calibrate` should refuse and say so (`--min-labeled 100`).

## 6. Order of work

1. **Stop counting silence as neutral** — the seeding at `1.0` is harmful
   before anything else is fixed.
2. `EVENT_WEIGHTS` → two axes.
3. `0x02`/`0x03` → `examined` / `observable`. ~15 lines; unblocks everything.
4. `decisions` + `decision_features` + `feature_counts`. Additive, droppable.
5. `--review`. The only writer of `pos`/`neg`.
6. `--calibrate`. Refuses to answer before day ~8.
7. `RefinementPlugin` **last, or not at all.** Until review has run two weeks,
   the free-text profile is a model narrating 40 samples it cannot evaluate.

## 7. Risks carried forward

- **Drift.** One wearer's interests move. The 7-day half-life should apply to
  the *displayed* weight while the *counts* stay cumulative — otherwise
  `pos`/`neg` churn and every weight returns to noise.
- **Cold start.** Days 1–8 have no threshold. Mitigate with a conservative
  default and an explicit banner. Do not interpolate toward the first observed
  label; at n≈30/day the first week is mostly noise.
- **Review fatigue → label bias.** If `y`/`n` becomes reflexive, labels stop
  being ground truth and everything downstream inherits the bias. Monitor the
  `s` skip rate — above ~20%, shorten the review before the data degrades.
- **Unverified on hardware:** the 8-second examination window and the
  re-examination rule both need real wear data. Both are schema columns, so
  they can be tuned by re-running SQL over logged history rather than by
  changing code.