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

---

## Option 1, taken

The injection was removed rather than defaulted off. The distinction is not
cosmetic: **a flag that disables nothing is worse than no flag**, because it
implies a supported configuration that does not exist and invites the next
person to spend an afternoon looking for a setting that has no effect.

What went:

| site | removed |
|---|---|
| `pipeline.generate_primary_response` | `preferences` param + the unguarded stage-1 imperative |
| `pipeline.condense_for_glasses` | `WEARER_CONTEXT_TEMPLATE` and its `context.mentions()` gate |
| `pipeline.answer_directly` | `topics` param |
| `pipeline.build_one_stage_system` | deleted outright; `ONE_STAGE_SYSTEM` is used directly |
| `pipeline.run_query` | `store`, `personalise`, `preferences` params, and the per-query `wearer_context()` scan |
| `session.Session` | `personalise` attr, `preferences()` on the ask path |
| `main.py` | `--no-personalise` |

The last row of that table is the one worth pausing on. Removing the
*injection* did not require removing the *ingestion*, but the ingestion had no
other purpose: it scanned the whole event history off the event loop — ~26ms
at 8000 events — once per ask and once per drill-down, to build a string the
model was then instructed to ignore. A mechanism whose only remaining product
is the cost of producing it should not survive its own removal.

### Two claims of different strength

The stage-1 removal is **not** backed by its own experiment. It follows by
consistency: it is the same mechanism, applied earlier in the pipeline, and
the version of it that was measured flipped answer polarity. Stage 2 can only
re-order facts the answer already contains; stage 1 decides what gets reasoned
about at all. So the measured result argues against the stage-1 site more
strongly than against the stage-2 one, and removing both is the consistent
move — but it is an inference, not a measurement, and it is recorded as one.

The substrate is untouched and is not in question: `topic_evidence()`,
`interests()`, `WearerContext`, `--weights` and `prefs` all still work, because
they measure cleanly. What was falsified is one specific act — reading those
numbers into a prompt.

### Correction: the substrate is not load-bearing either

While writing the removal I claimed in a code comment that the same
information "still steers the compression in ONE place", by way of the
interruption queue. Checking it before committing, that was false. After this
removal the topic weights have exactly **one** consumer in the shipped
package: `main.py --weights` and a log line in `Session._record`. They do not
rank the queue — that gate reads `queue_worth_rate` over review labels
(`policy.py`) — and they touch no prompt. Per-card `keywords` are stored and
have no shipped consumer either.

So the accurate description is narrower than "the substrate stays useful": it
is measured, logged, and currently load-bearing for nothing. That matters for
what comes next. A clean substrate is a good foundation and a poor argument.
Whatever eventually reads these weights should be measured on its own terms,
not treated as already-vindicated because the thing standing on top of it
once failed. The refiner is the cautionary case in miniature: it stood on this
same substrate, was described as working, and was never connected to anything.

### The re-run disagrees on the sign, and that is the finding

After the removal I re-ran this experiment — same model, same corpus, same
seeded store — to check the falsification would reproduce. It splits cleanly:

| claim | run 1 | re-run |
|---|---|---|
| gate fires on ordinary questions | 0/8 | **0/8, exact** |
| latency cost | 0.44s vs 0.25s | **0.46s vs 0.33s** |
| conditional effect (n=3) | polarity flipped | led with the interest fact **3 vs 2** |

The third row is the one to be honest about. Run 1 found the block changing a
"no" into a "yes" — a failure. The re-run landed on the branch that reads as
a small *win*. Both are real outputs, and at n=3 the experiment cannot tell
them apart; this document's own verdict section said as much when it was
written ("n=3 is far too small to conclude anything").

So the removal does not rest on the polarity observation, which is
single-run. It rests on the two rows that reproduced: the gate is inert in
normal use, and the block is expensive whenever it is not. A feature that
never fires, costs latency on the rare query where it does, and produces
unstable conditional effects on the strength of n=3 has no positive result to
preserve. If someone wants to bring it back, the burden is a corpus where the
gate fires often enough to measure — not a third run of this one.

The instability across identical calls is the substantive finding. It was
already visible in run 1, where the block's agreement with itself was 0/3
while the plain arm's was 2/3. A mechanism that changes the answer
unpredictably, in a system whose one hard promise is that the wearer can act
on what they read, is a worse default than no mechanism.

### The third injection site was never connected at all

Removing the prompts turned up something the A/B could not have found.
`plugins.RefinementPlugin` is a *third* personalisation mechanism: it asks a
model to distil the reaction history into durable summarisation preferences,
caches them under `refined_profile`, and its docstring claimed the cached
text "is what gets injected into the stage-2 prompt from then on."

That sentence was false for the entire life of the code.
`plugins.preferred_profile()` — the only consumer of the cache — has never had
a caller in any commit of the repository, verified with `git log -S`. So while
the stage-2 prompt was being shaped by one block, a second mechanism sat
beside it, described as live, wired to nothing.

It is kept as an extension point, with the false claim corrected, and it is
not wired to a prompt. The lesson is not about the plugin; it is that the
project had *three* answers to "what personalises this?" and only one of them
was ever measured. The two that were not measured turned out to be one that
hurts and one that does not exist. Everything here would have been found
sooner by a single check for whether a function has any callers.

### Keeping the falsification reproducible

`experiments/personalisation_ab.py` now carries its own copy of the removed
block. Reproducing the measurement means reproducing the arm in the
experiment rather than importing it from the shipped package, which is the
correct direction of dependency: a falsification should not be able to rot
when the thing it falsifies is deleted.

`experiments/adaptive_loop.py` §2 and §3b were inverted. They previously
asserted that an on-topic query *received* the block; they now assert that no
prompt on either architecture contains one. Both sections open with a
planted-block positive control, because an absence asserted by a search
string that cannot match is indistinguishable from a pass. A throwaway
mutation test re-injected the block into each of the three prompts and
confirmed each is caught by the assertion covering it.

The eighth rule was never the problem. It was, in the end, five wordings of
the same block, and each revision added another clause about how not to act. A
mechanism that needs eight rules telling it to be harmless is already acting.