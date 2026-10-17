# TURN B — Can a decision model verify glanceability before display?

**Question.** The re-examination signal (head-up, head-down, head-up) tells you a
summary was not readable at a glance — but only *after* the first display. Can
`winnow:e4b` predict it *before* showing, and does it beat a four-line regex?

**Answer in one line.** Yes with a real but narrow margin — AUC 0.838 vs 0.688
for the heuristic (paired bootstrap Δ=+0.150, 95% CI [+0.036, +0.285],
P(winnow better)=0.997) — but the model verifies *concreteness*, not
*glanceability*: it is completely blind to fact burial, as is the regex.

All numbers below are from real runs on this machine (WSL2, RTX 4050 6 GB) on
2026-10-16. Scripts and raw data: `/tmp/glance_testset.json`,
`/tmp/run_winnow_glance.py`, `/tmp/winnow_glance_results.json`,
`/tmp/analyze_glance.py`.

---

## 1. Test set — 50 items, labels fixed before any winnow run

10 organic summaries were generated with the repo's own two-stage pipeline
(`pipeline.py`, stage 1 + stage 2) on `qwen2.5-coder:7b` via ollama
(`/tmp/gen_summaries.py`): **70 s total for 10 queries**, i.e. ~7 s per
two-stage summary. `tev1:latest` could not be used — it has no chat template
(`"tev1:latest" does not support chat`, error 400). The organic outputs were
realistically imperfect (e.g. `Your passport expires on [insert specific
date].`, `Check your calendar or ask a digital assistant.`), which is exactly
the failure surface a gate must catch.

Each item has a pre-declared **single most important fact** (SMIF). Label 1 =
SMIF recoverable from the summary alone, concrete, within the first ~48 chars
(first two of five 24-char lines — the glance-acuity premise, asserted, not
measured on hardware). Label 0 = SMIF absent, referent-only, hedged, or buried
in the final line. 25 positive / 25 negative across 8 categories:

| category | n | label | point |
|---|---|---|---|
| organic (pipeline output) | 10 | mixed | real stage-2 failures |
| concrete-lead | 10 | 1 | easy positives |
| pronoun-lead ("This was fixed…") | 5 | 0 | referent failure |
| hedged (no number/name/deadline) | 5 | 0 | empty prose |
| buried-deadline (fact in last line) | 5 | 0 | position failure |
| no-digits-good ("Rain starting now…") | 6 | 1 | **decouples concreteness from surface features** |
| digits-vague ("Regarding case 4471… later point") | 5 | 0 | **decouples in the other direction** |
| organic-fixed (hand-repaired organics) | 4 | 1 | repairs as positives |

The last three categories exist to break circularity: if "bad" is defined as
"no digits", the regex wins by definition. The hard items make digits and
glanceability point in opposite directions.

## 2. The winnow question (frozen)

Prompt-tuned on **4 dev items only** (2 easy, 2 decoupled-hard), then frozen
before the test set was scored — otherwise the prompt would be overfit to the
eval. Tuning mattered: my first phrasing (long compound instructions) scored a
clearly-vague summary *above* a clearly-good one (grade-2 prob 0.63 vs 0.55).
The frozen version, `/tmp/q-v2.json`:

```json
{
  "glanceable": {
    "type": "score",
    "criteria": [
      "0: not recoverable in one glance",
      "1: partly recoverable",
      "2: fully recoverable in one glance"
    ],
    "instructions": "Display text for smart glasses, read in one second while walking. Can the main fact be recovered from the SUMMARY alone at a glance? Referents like This or It, hedges, and buried facts score 0-1. Numbers, names, times up front score 2."
  }
}
```

Two conditions per item: `src` (state = `SOURCE: …\nSUMMARY: …`, what a
pre-display gate would actually have) and `summ` (summary alone). Real command:

```
ollaya run winnow:e4b --questions /tmp/q-v2.json --format json --keepalive 10m \
  "SOURCE: Train 8814 … departs 09:12 … delayed to 09:40.
SUMMARY: Train 8814 Osaka delayed to 09:40, platform 6."
```

## 3. Results

100/100 calls succeeded. Rank AUC (Mann-Whitney), bootstrap 95% CI, n=50:

| judge | AUC | 95% CI |
|---|---|---|
| **winnow, src+summary** | **0.838** | [0.716, 0.937] |
| winnow, summary only | 0.760 | [0.611, 0.893] |
| heuristic (regex) | 0.688 | [0.534, 0.826] |

Paired bootstrap of the difference (the CIs above overlap, so this is the test
that matters): **ΔAUC = +0.150, 95% CI [+0.036, +0.285], P(winnow > heuristic)
= 0.997.** The bar is cleared — but not by much, and only on a set I built.

At the best fixed threshold (score ≥ 1.18, accuracy 0.80): TP=22 FP=7 TN=18
FN=3. The error structure is the finding:

- **All 5 false positives are the entire buried-deadline category, plus g2/g5
  (digits-vague).** Winnow scored the buried items at mean 1.68 — higher than
  the easy positives' organic-fixed (1.29) — because the fact *is present and
  concrete*, just in the last rendered line. It checks "is the fact there",
  not "is it in the fovea".
- The false negatives are the noisy borderline organics (o2: fact in 4 words
  then 90 chars of checklist; o8: telegraphic) — defensible rejections.

Source context helps (+0.078 AUC), mostly on honest no-data summaries: "No
weather feed. Will retry in 10 min." scores 0.61 alone, 1.31 with source —
only the source tells you "no data" *is* the fact. The gate should see stage-1
output, which it already has for free.

Confidence is **not** a usable gate signal: mean 0.54 on correct judgments vs
0.47 on incorrect. Confirms the docs' warning that winnow's confidence is
uncalibrated; use the score, ignore the confidence.

## 4. Real latency and cost

| quantity | measured |
|---|---|
| warm call, sustained (GPU clocks hot), median / p90 | **0.110 s / 0.114 s** (n=50) |
| warm call from idle GPU (single probes, earlier) | 0.505–0.530 s |
| cold: model load + first decision | **3.55 s** |
| input size | median 204 tokens (src condition) |
| errors | 0 / 100 |
| VRAM with winnow resident | 5.8 / 6.1 GiB (nvidia-smi) |

The documented "~22 ms warm" is not reproducible at realistic state sizes —
a ~200-token state costs 110 ms sustained, ~500 ms from idle. Still cheap
next to stage 1 (8.4 s) — 1–6% overhead as a serial gate.

The real cost is **VRAM contention, not latency**: winnow resident means
qwen2.5-coder (4.0–4.7 GB) cannot be (5.8 + 4.0 > 6.1 GiB). A serial
gate between stage 2 and display forces a model ping-pong: unload qwen →
load winnow (3.55 s, measured) → gate → unload winnow → reload qwen for the
next query. Unless queries cluster inside the 10-minute keepalive, the gate's
true marginal cost is seconds of swap per query, not 0.11 s. I ran the
experiment exactly this way, manually.

## 5. The heuristic baseline (frozen before seeing winnow output)

Exactly the four proposed features: +1 contains a digit; +1 contains a
capitalised non-sentence-initial word (proper-noun proxy); +1 contains a time
expression (regex over clock times, weekdays, today/tonight/morning…);
− min(words before first concrete token, 6)/6, where concrete = digit-bearing,
proper-noun-proxy, or in a ~60-word fixed noun lexicon. ~25 lines of Python,
no tuning. **AUC 0.688.**

Where it loses to winnow: `no-digits-good` (mean heuristic score +0.11 on
*positive* items — it calls "Rain starting now. Umbrella is in the hall
closet." bad; winnow: 1.68). Where it ties or wins: nowhere decisive — but a
post-hoc variant adding "first digit/time must appear in the first 48 chars"
only reached 0.701 (tuned on test, not a fair number), because position-of-
digit penalises the digit-less positives too. Position of the first *concrete
token* is the right feature, and that is precisely what a word-list can't
approximate and a language model can.

## 6. Verdict

**A pre-display glanceability check is worth it, in a specific shape.**

1. **Ship the regex, not winnow, as the *blocking* gate.** At 0.688 AUC and
   zero latency/VRAM cost it catches referent-lead and hedged failures — the
   two categories where the summary genuinely lost the fact. Vagueness is
   cheap to detect; burial is not detected by either judge.

2. **Winnow's value is offline, not inline: run it *after* display as a
   logging judge** (score each shown summary, store it in the ledger with the
   schema's decision fields). This dodges the VRAM ping-pong entirely, costs
   nothing on the display path, and — because the score separates the good
   from the bad (0.838) — turns every later re-examination event into a
   calibration point for *both* the regex and winnow. That is the only path
   to real labels, which this experiment conspicuously lacks.

3. **When the check says "not glanceable": repair, don't suppress.**
   - *Buried fact* (the regex can find a digit/time in the last line): a
     deterministic reorder — move the clause carrying the number/time to the
     front — fixes category E mechanically. No model needed, and both judges
     are blind here, so the rule does work nothing else does.
   - *Vague/referent summary*: the fact still exists in stage-1 output (the
     gate has it). One stage-2 retry with the feedback "previous attempt
     omitted the concrete fact — lead with <fact>" (0.8 s, measured) is
     justified; cap at one retry, then fall back to deterministic
     extraction of the SMIF-bearing sentence.
   - Suppress only if both attempts fail — silence is the default state.

**Critical assessment.** The labels are mine, constructed by design; n=50;
the prompt was tuned on 4 dev items by me; the organic summaries came from a
model with fabricated data (no real calendar), so the mix is rougher than
production. The "first 48 chars" premise is asserted from glance-reading
behavior — nothing here has been measured on the actual G1 (WSL2 has no BLE
path yet, per the docs' known blocker). The paired-bootstrap margin (+0.15)
would likely shrink on adversarial or production-distributed text. And the
result that "decision model beats regex" is true only because the test set
contains decoupled hard cases; on a naive set of hedged-vs-numeric summaries
the regex ties. The durable findings are the blind spot (position) and the
cost structure (VRAM swap, not latency), not the exact AUC.
