# Personalisation, measured: the block does not earn its place

The A/B that `.context/llm.md` has listed as an open falsification since the
feature was built. It stayed unrun because the flag did not exist; then because
the architecture moved and the stage-2 prompt stopped being sent. Both reasons
are gone, so it ran.

**Verdict: the feature should not ship as designed.** Not "needs tuning" —
the measurements say two different things are wrong with it, and neither is a
threshold.

Reproduce:
```bash
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama \
  OPENAI_MODEL=qwen2.5-coder:7b .venv/bin/python experiments/personalisation_ab.py
```

---

## 1. The salience gate is nearly inert

Two corpora, because the first run of this experiment found the answer by
accident and it needed separating from the effect measurement.

**Ordinary questions** — 8 of them, phrased the way people actually ask:

```
what happened with the build?
tell me about the migration
how did the interview go?
did the payment go through?
```

**0 of 8 passed the gate.**

The gate requires the *question* to literally contain an interest word. "What
happened with the build?" will never contain "rollback". So on ordinary traffic
the block is not sent at all — not "sent and ignored", **not sent**.

This is compounded by a second limit found while writing the experiment:
`wearer_context()` caps interests at **3** (`interests(limit=3)`). A wearer with
twelve evidenced topics gets three of them in the prompt, and the gate can only
fire when the question names one of those three. In the deliberately
topic-bearing corpus, questions naming "rollback", "priority" and "salary" were
still gated off — because those three topics had fallen outside the top-3 cut.

**A gate that never fires is indistinguishable from a feature that does not
exist**, except it is harder to notice, because every log line still says the
context was ingested.

## 2. When it does fire, it moves the answer — which its own rules forbid

3 of 8 topic-bearing queries passed the gate. At n=3 the headline comparison is
noise (interest fact present 3/3 with the block vs 2/3 without). But re-running
the two firing cases three times each says something the headline does not:

```
Q: has the refund been issued?
  WITH block : 'No, the refund has not been issued.'      <- inconsistent
               'Yes, the refund has been issued.'
  WITHOUT    : 'No, the refund has not been issued yet. …' x3  <- consistent

Q: is there a firmware update pending?
  WITH block : 'Yes, firmware update available.'         <- polarity shifted
               'Yes, firmware update v2.3 is pending.'
  WITHOUT    : 'No firmware update pending.'
               'No update pending.'
               'Yes, firmware update v2.3.1 is pending.'
```

The block does not reorder facts. It **shifts the polarity of the answer** and,
in the refund case, makes the answer *unstable across identical calls*.

That is precisely what rule 9 of the block forbids — "never introduce, infer,
retrieve or go looking for a fact because a subject is listed here" — and rule 7
promises interests may only "break a tie BETWEEN facts the answer already
contains". They are not doing that. Mentioning "refund" makes the model more
likely to say the refund happened.

### A correction to my own first reading

On the first run I concluded the block had invented a firmware version number
("v2.3"), violating rule 9. **That was wrong.** Re-running showed the *without*
arm inventing "v2.3.1" too. The version-number hallucination belongs to the
base model at temperature 0.2, not to the interest block. It is not evidence
against the block and I should not have reported it as such.

What survives the correction is narrower and still sufficient: the block shifts
polarity, and destabilises an otherwise stable answer.

## 3. It costs latency

```
median latency   with block 0.44s   without 0.25s
```

The system prompt grows by the interest block and the fixed overhead lands in
every call. Against a budget that was just tightened from **6.71s to 0.48s**
median by removing an entire model call, spending 76% more wall time on a
feature that measurably does not help is a poor trade.

---

## What follows

The honest options are, in order of my preference:

1. **Remove the injection.** It fires on 0/8 ordinary queries, changes answers
   in a way its own rules forbid, and costs latency. The learning substrate
   (`memory.py`, `topic_evidence`, `interests`, `--weights`) is sound and
   genuinely useful for *observing* the wearer — it is only the act of telling
   the model about it that fails.
2. **Keep it off by default**, flip `personalise=True` when a wearer has
   enough history for the gate to fire on *their* phrasing. This is the current
   default-on state, which the data does not support.
3. **Re-gate on the answer, not the question** — the two-stage design's gate.
   That was possible only when stage 2 existed; one stage made it impossible,
   which is a cost of the architecture flip that was not accounted for.

Option 1 is what the evidence supports. It is also the outcome this project has
reached before — a signal that measures cleanly in isolation (the substrate
experiments all pass) and then does nothing at the point of use. That pattern
is now documented in `documents/15` and here; `documents/13` said the same of
the 0.813 AUC before either existed.

## What this does NOT say

- Not that topic weighting is worthless. `topic_evidence()` is measurably
  sound: no saturation, evidence-gated, correctly ordered.
- Not that a smarter gate would not work. It might — but "the gate might be
  improvable" is not evidence, and the project has a documented history of
  treating plausible mechanisms as if they were measured ones.
- n=3 for the conditional effect. Everything above rests on a very small
  number of firing cases, which is itself the point: the feature rarely fires.