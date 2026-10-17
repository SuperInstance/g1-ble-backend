# Round 2 — one design, converged (zcode)

Premises locked by measurement, not preference: winnow resident holds 5826/6141 MiB, so the tier split is VRAM arithmetic, not battery; the firmware threshold θ is unmeasured, so every examination-signal claim is conditional on ρ = θ/A_b ≥ 1.6; n=40 (A) and n=50 self-labeled (B) can falsify but cannot tune — only review labels (900/month) can.

## 1. The decision path: event → lens, or nothing

**Desk tier, per candidate, batched on a 5-min cycle. Nothing here is latency-critical.**

1. **Ingest.** `~/.g1/outbox/*.json` via inotify → `candidates` row. Cost ~0.
2. **G1 — noise suppression, winnow batch score.** Winnow is the only resident model; score the whole batch at 110 ms/item sustained. Suppress at `s < 0.30` (noise median 0.258, glance median 0.700, Spearman 0.737 monotone). Suppressed rows stay reviewable (stratum A). Then **unload winnow**.
3. **G2 — tier/push gate, deterministic rules; zero-shot models banned.** Push iff: (a) starred human + explicit deadline, or meeting at T-10 min; (b) own-agent job dispatched this session, elapsed >2 min; (c) device-state transition. Budget ≤2/h, cap 4/day; one mis-fire suspends the source class 24 h. Everything else queues silently.
4. **Generation.** qwen2.5-coder (now loaded) builds `display_text` ≤120 chars, ~7 s/item two-stage; tev1 is cut. One model ping-pong per batch (3.55 s reload), amortized across the batch — never per item.
5. **G3 — glanceability, the 25-line regex, blocking.** Features: digit, proper-noun proxy, time expression, position of first concrete token. On fail: **repair, never suppress** — (i) number/time in last line → deterministic reorder to front; (ii) vague/referent → one stage-2 retry (~0.8 s) with "lead with the fact"; (iii) SMIF-sentence fallback; (iv) drop only if both fail.
6. **Offline judge.** Winnow (resident again) scores each *shown* text into a logging column. Never gates; confidence column ignored (0.54 correct vs 0.47 incorrect).
7. **Sync.** Rows + nullable `model_score`, `detail_text` written to Pi SQLite at boot and on the 5-min poll. NULL never gates anything.

**Walking tier — 0.0023% of one core, zero models, zero network.**

8. Head-up (`0x02`) → **S1 dwell hysteresis, dwell 500 ms / release 400 ms**, calibrated at install to `max(500, 1.5×p99)` from a 10-min no-glance walk. Adds ~325 ms p50.
9. Confirmed glance in IDLE → **PEEK**: line 1 `3 new · 14:05`, lines 2–5 top-4 items, sender+truncated subject, **no LLM**. Successive full-screen writes (paging is broken). Head-down or 8 s → IDLE.
10. Tap → **ITEM**: `display_text` if non-NULL else the queue line. NULL detail → `· no detail` + `drill_miss` row.
11. Every gaze episode is logged; nothing at runtime reads it. Ground truth comes from review, not the device.

If G1 suppresses, G3 drops after repair, or nothing queues: nothing happens. Silence is the default state.

## 2. Do 0.958 and 0.813 justify two gates? Yes — but correct the numbers first

The 0.958 is noise-vs-*interrupt*. The auto-suppress gate actually separates noise from *queue-worthy* (glance+interrupt), whose honest AUC is **0.898** (noise-vs-glance alone is 0.838). That's still adequate — because G1's errors are cheap (a suppressed item costs a queue slot; recall ≥0.90 and the queue are the safety net) and a conservative τ only has to catch the noise bulk below the medians.

The 0.813 boundary is different in kind, not degree: it is subjective and context-dependent (A2 says so; the falsified clock fix proves the residual errors are tier-assignment, not information), and the budget there is precision ≥0.99, which A measured as reachable only at recall 0.167. No threshold on any zero-shot score meets it.

So: two gates, **two mechanisms**. G1 = model threshold, errors cheap, target objective. G2 = deterministic eligibility rules now, per-wearer calibrated weights later (day ≥8, ≥100 labels, plateau-reported, refuses before that). The justification is not the AUC gap alone but the cost asymmetry: a G1 error costs a slot; a G2 error costs trust — abandonment risk #1. A model is not allowed near G2 precisely because 0.813 < what 0.99 demands.

## 3. Minimum with no desk link

The Pi must be fully correct alone for a day: BLE central both arms with preflight (adapter Powered+central, `mtu_size > 23` after `_acquire_mtu()`), 10 s heartbeat, bounded queue (≤12 active, 4 visible, source-collapse, TTL, eviction logged `overflow`), S1 debounce + episode WAL, PEEK/ITEM/tap machine, and the **liveness line on head-up even when the queue is empty** — that is what keeps "desk down" distinguishable from "BLE dead." Everything rendered is precomputed; nothing blocks on the desk; degradation is toward silence, the correct direction. Generation, scoring, judging, detail: desk-only and nullable.

## 4. Cut list

- **S2 and S3 debounce.** S2 is S1 with a worse constant; S3 at 2.2 s refractory manufactures 31% re-examinations; at 4 s it's S1 with more state.
- **Clock/deadline feature.** Falsified: zero precision gain, vetoes worse.
- **tev1:latest.** No chat template.
- **Winnow confidence; winnow as any blocking gate; winnow off-desk.**
- **Refiner / RefinementPlugin / free-text profile.** A model narrating 40 samples it cannot evaluate. Cut from M1 outright.
- **Voice (0xF1/0x0E).** A second input path competing for the same attention budget.
- **DETAIL state in M1.** `drill_miss` logs the demand; build only if the logs ask.
- **Live push in M1.** No source is trusted at 0.99 yet; collect tier labels via review.
- **Every candidate source except the agent spool.** No OAuth, no integrations, until one source yields a fortnight of labels.
- **E-C Bandit over implicit signals.** 7-day half-life vs 10–41 day convergence is non-convergence (08 §2). Learners wait for explicit labels.
- **Scalar `topic_weights`/`needs_detail` double-count, `star` dead code, the fictional `detail="touchbar/triple-tap"` provenance** (08 §0).

## 5. M1 — the two-week outcome

**"Spool to lens."** One source (own agents), queue-only, no push, nightly stratified review (A uniform, B never-examined) under 60 s/day. Day 1: the 10-minute calibration walk — it settles ρ, BLE preflight, and MTU in one session. Day-14 verdict, one query:

```sql
WITH last7 AS (SELECT * FROM impressions
               WHERE day >= (SELECT max(day) - 7 FROM impressions))
SELECT (SELECT round(avg(r.worth), 2) FROM review_labels r
         JOIN last7 i USING (candidate_id))              AS queue_worth_rate,
       (SELECT count(*) FROM last7 WHERE opened = 1)     AS item_opens_7d,
       (SELECT round(count(*) / 7.0, 1) FROM episodes
         WHERE kind = 'peek'
           AND day >= (SELECT max(day) - 7 FROM episodes)) AS peeks_per_day;
```

**Continue iff:** ρ ≥ 1.6 measured (else examination labels are fiction — pivot to TouchBar-only or stop); `queue_worth_rate ≥ 0.5`; peeks/day not decaying week 1 → week 2; review skip rate < 20%. Any failure names the thing to change or the reason to stop. It is an outcome — did a worn queue earn glances for two weeks on real labels — not a platform.

## 6. Disagreements, loudest first

1. **The weakest claim in the corpus is anything resting on the examination signal before θ is measured.** B's re-examination-as-quality-signal and 08's whole `examined` cross-tab ("40% of confirms never examined") inherit C's finding that at ρ ≤ 1.25, 8–18% of examinations are fabricated and *every one is contaminating*. C states this — then B and 08 build on the signal anyway. Until the day-1 walk, `examined` is unproven (05's caveat stands).
2. **C's "825 ms head-up→lens, probably imperceptible."** Asserted, never worn. And C's own link-loss table shows its recommended 500 ms dwell still leaks 12.8 false/h at 4% loss — C underplays that its fix doesn't reach zero either.
3. **B's +0.150 AUC margin.** Labels constructed by the same author who tuned the prompt on 4 dev items; the durable findings are the burial blind spot and the VRAM cost structure, not the AUC. B's offline judge must join the batch ping-pong or it re-pays 3.55 s per item.
4. **A2's "personal-calibration problem."** Plausible, unproven; the alternative is that the 0.813 boundary is unlearnable noise. Day-14 labels settle it — don't pre-commit a learner to it.
