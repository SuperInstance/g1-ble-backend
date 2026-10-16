# Turn A — can a decision model be the interruption gate?

Model: `winnow:e4b` via `ollaya serve` (HTTP `/api/decide`).
Corpus: `experiments/corpus.json`, 40 candidates, base rate 0.300.
Schema: `experiments/worth-interrupting.json`.

## Verdict

**No. Not as the gate.** The score is a genuinely useful *feature* but it cannot carry the precision budget alone.

## Discrimination

| metric | value |
|---|---|
| AUC, bucket-2 vs rest | **0.8958** |
| AUC, bucket>=1 vs noise | 0.8984 |
| best precision at recall >= 0.50 | **0.875** |
| best precision at recall >= 0.90 | **0.688** |
| precision >= 0.99 reachable only at recall | 0.167 |

The design budget set by the interaction panel was **precision >= 0.99**.
The best achievable here at a usable recall is **0.875**. That is a
failure margin of roughly one false interruption per eight — enough to
cost trust within a week, which the same panel identified as the primary
abandonment risk.

## The score ranges physically overlap

- noise (bucket 0) reaches **0.727**
- urgent (bucket 2) starts at **0.229**

No threshold on this single number can separate them. The worst-scored
urgent item scores below the third-best-scoring piece of noise.

## Precision/recall curve

| threshold | relayed | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|---|
| 1.918 | 1 | 1 | 0 | 11 | 1.000 | 0.083 |
| 1.822 | 2 | 2 | 0 | 10 | 1.000 | 0.167 |
| 1.806 | 3 | 2 | 1 | 10 | 0.667 | 0.167 |
| 1.731 | 4 | 3 | 1 | 9 | 0.750 | 0.250 |
| 1.730 | 5 | 4 | 1 | 8 | 0.800 | 0.333 |
| 1.698 | 6 | 5 | 1 | 7 | 0.833 | 0.417 |
| 1.430 | 7 | 6 | 1 | 6 | 0.857 | 0.500 |
| 1.347 | 8 | 7 | 1 | 5 | 0.875 | 0.583 |
| 1.171 | 9 | 7 | 2 | 5 | 0.778 | 0.583 |
| 1.012 | 10 | 8 | 2 | 4 | 0.800 | 0.667 |
| 0.981 | 11 | 9 | 2 | 3 | 0.818 | 0.750 |
| 0.961 | 12 | 10 | 2 | 2 | 0.833 | 0.833 |
| 0.956 | 13 | 10 | 3 | 2 | 0.769 | 0.833 |
| 0.860 | 14 | 10 | 4 | 2 | 0.714 | 0.833 |

## What the model got right

It is **not** fooled by tone, capitalisation or punctuation. The trap set
was built specifically to break a scorer that keys on surface cues:

| text | truth | score |
|---|---|---|
| quick one: prod database is down | 2 | 1.7309 |
| minor: your access expires tomorrow morning | 2 | 1.8217 |
| FYI your password expires in 2 hours | 2 | 1.7295 |
| fyi no rush on this at all | 0 | 0.0563 |
| Heads up, nothing on fire, low priority housekeeping | 0 | 0.1415 |
| !!! URGENT !!! please read when convenient | 0 | 0.7046 |
| everything is fine, no action needed today | 0 | 0.0443 |

Loud-and-empty scored 0.70; quiet-and-urgent scored 1.73. That is the
behaviour you want and it is not trivial.

## Latency — two documented claims are now falsified

| | measured | previously claimed |
|---|---|---|
| cold call | **19.06s** | not measured |
| warm median | **145ms** | "~22 ms" |
| 40 calls total | 24.8s | |

1. **The 22 ms figure was wrong by ~7x.** It was a single observation on
   one short input. Warm median is ~145 ms. Still cheap enough to sit
   off the latency-critical path, but it is not free.
2. **`winnow:e4b` does run on this machine despite being 8 GB against
   6 GB of VRAM.** Cold load 19.1 s, then warm. The operational panel's
   claim that it "never fits" is falsified — it must be offloading, and
   the correct statement is that it costs 19 s to load and ~145 ms warm.
   Whether it *shares* the GPU with a 7B generator is still unmeasured.

## What this means for the design

1. **Do not use winnow alone as the gate.** Keep it as one feature
   among several. The PR curve above is the evidence.
2. **The failure mode is bucket confusion, not tone.** Bucket-1 items
   (glance-worthy) are being pulled into interrupt territory because
   they contain a time or a number — "Battery at 22%", "Backlog
   grooming moved to 11:00", "Stripe deprecating ... 2026-11-01".
   A deadline *relative to now* would separate these from
   "expires in 2 hours". That is a **feature the decision model does
   not have**: it sees text, not a clock.
3. **Add a clock.** A deadline-extraction rule computed locally is free,
   exact, and attacks precisely the confusion this experiment found.
4. **The corpus is 40 items.** Enough to falsify a claim, nowhere near
   enough to tune a threshold. AUC 0.896 is a reason to keep the model
   in the loop, not a number to ship.

## Reproduce

```
ollaya serve                                   # already running on :11435
.venv/bin/python experiments/score_corpus.py
.venv/bin/python experiments/report-A.py
```
