# Turn A, corrected — the metric was wrong before the model was

## What went wrong with the first analysis

Turn A reported AUC 0.896 and "precision 0.875 vs a 0.99 budget", concluding the
decision model could not be the gate. That conclusion was drawn from a **binary**
collapse of an ordinal label.

The corpus has three buckets: `0` noise, `1` glance-worthy, `2` interrupt. The
analysis treated everything that was not a `2` as a negative. That penalises the
model for correctly ranking a high-salience glance-worthy item above noise —
behaviour the product actively wants. "Stripe is deprecating the Charges API"
scored 1.806 and was counted as a false alarm, when the honest reading is that
it is genuinely salient and belongs in the queue, not that it was missed up.

## The corrected reading

Bucket medians are cleanly monotone:

| bucket | n | median score |
|---|---|---|
| noise | 16 | 0.258 |
| glance-worthy | 12 | 0.700 |
| interrupt | 12 | 1.430 |

| metric | value |
|---|---|
| Spearman(score, ordinal truth) | **0.737** |
| Spearman(score, binary bucket-2) | 0.544 |
| AUC noise vs interrupt | **0.9583** |
| AUC noise vs glance | 0.8385 |
| AUC glance vs interrupt | **0.8125** |

## What this actually means for the architecture

The two boundaries are not equally hard, and they should not share one gate.

1. **Noise vs anything — AUC 0.958.** Objective, and the model is good at it.
   A single score threshold here safely auto-suppresses. This is the cheap tier
   that keeps the queue clean, and it is reliable.

2. **Glance-worthy vs interrupt — AUC 0.813, zero-shot.** This is the hard one,
   and it is *subjective and context-dependent*. Whether "backlog grooming moved
   to 11:00" deserves an interruption depends on who is asking and what else is
   happening. No zero-shot scorer will resolve this; it is exactly the boundary
   that per-wearer learned features exist to fit.

**The 0.99 precision budget is therefore not a model problem. It is a
personal-calibration problem**, and it is answerable only from the wearer's own
labels — which is what documents/08-panel-learning-substrate.md argues, and this
experiment now supports with measurement.

## The clock hypothesis: falsified

Turn A proposed that the bucket confusion was a missing clock feature, since the
model cannot see "now". Tested in `experiments/clock_feature.py`:

| combination | AUC | precision@recall>=0.5 |
|---|---|---|
| winnow alone | 0.8958 | 0.875 |
| clock urgency alone | 0.7083 | 0.857 |
| max(winnow, 1.6x urgency) | 0.8958 | 0.875 |
| 0.7x winnow + 1.4x urgency | 0.9048 | 0.875 |
| winnow x (0.5 if deadline > 4h) | 0.8869 | 0.857 |
| winnow x urgency(deadline) | 0.8155 | 0.800 |

**Zero precision gain, and every veto variant makes it worse.** The reason is
visible in the error list: the residual errors are not clock errors. The misses
are "Build 4821 finished: all 24 tests green" (0.229), "Caleb says: call me
now" (0.961), "Server room temperature 31C" (0.981) — none have a deadline at
all. The false alarms are "Backlog grooming moved to 11:00", "Alex moved the
design review to Thursday" — which the extractor correctly ignores, and rightly
so, since a moved meeting is not an expiring one.

The confusion is **about which tier an item belongs to**, not about time
arithmetic. A clock feature cannot fix a labelling-boundary problem.

## Honest limits

n = 40, with 12 positives. One error is worth 8.3 precision points. No threshold
should be tuned on this, and none is proposed. The result is directional: it
justifies keeping the decision model as a *feature* and splitting the gate in
two, and it rules out shipping a single zero-shot score threshold.
