# DESIGN — the G1 relay agent

Synthesised from two convergence rounds: a four-model design panel
(`documents/02`–`08`) and three hands-on hardware experiments
(`documents/10`, `12`, `experiments/`).

Where the reviewers disagree, the disagreement is preserved rather than
averaged away. Read the "Contested" section before building anything.

**Status: no G1 hardware has ever been exercised.** The development host is
WSL2 with no BlueZ (`org.bluez was not provided by any .service files`). Every
protocol claim is inferred from the official demo app and community notes. The
single highest-value next action is a ten-minute walk with the glasses on.

---

## 1. The thesis

**Silence is the default state. The drop path is the most important code.**

An interruption costs 4–8 seconds of recovered attention. At the proposed
budget of ≤2/hour that is ~1.7% of waking time. Above ~10/hour the glasses
become a watch that nags and gets taken off — and per the interaction review,
"what kills this product" is *wrong items, reliably*, not bad compression.

Budgets, unchanged across both convergence rounds:

| | |
|---|---|
| Push budget | ≤2/hour, hard cap 4/day |
| Precision | ≥99% (asymmetric: a miss costs nothing, the queue is the safety net) |
| Recall | ≥90% |
| Trust collapse | one mis-fire auto-suspends that source class for 24h |

---

## 2. The decision path

Two hosts. **Walking tier** (Pi-class, BLE + rules only, measured at **0.0023%
of one core**, 136 800 rows for a ten-hour walk). **Desk tier** (this machine:
generation, scoring, the nightly review). The split is forced by **VRAM, not
battery** — winnow resident holds 5826/6141 MiB.

### Desk tier, batched every 5 minutes — nothing here is latency-critical

| # | Gate | Mechanism | Cost |
|---|---|---|---|
| E0 | Ingest `~/.g1/outbox/*.json` via inotify | fs | ~0 |
| **G1** | **Noise suppression.** winnow score, suppress `s < 0.30` | model | 110 ms/item sustained |
| **G2** | **Push eligibility.** Deterministic rules only | rules | µs |
| **G3** | **Glanceability.** Regex blocking gate → repair | regex + rules | ~0 |
| — | Offline judge: winnow scores each *shown* text into a nullable logging column | model | batched |
| — | Sync rows to the Pi SQLite | fs | boot + 5 min |

**G2 rules, exhaustively:**

- **meeting** — accepted, at T-10 min
- **blocked_job** — `blocking=true` and `elapsed_s > 120`
- **device** — battery drops below 10%

Everything else queues silently. **Nothing is ever dropped.**

### Walking tier — no models, no network, no dependency on the desk

1. BLE central, both arms, preflight: adapter Powered + central, `mtu_size > 23`
   after `_acquire_mtu()`, autosuspend disabled.
2. 10 s heartbeat (the link dies after ~32 s idle).
3. Head-up debounce: **500 ms dwell / 400 ms release**, calibrated at install to
   `max(500, 1.5 × p99)` from a ten-minute no-glance walk.
4. `IDLE → PEEK` on a confirmed glance: line 1 = `3 new · 14:05`, lines 2–5 the
   top four items as **sender + truncated subject, no LLM**. Head-down or 8 s → IDLE.
5. Tap → `ITEM`. Successive full-screen writes — **paging does not work**
   (`page`/`max_page` index screens, not BLE fragments).
6. Every gaze episode is logged. **Nothing at runtime reads it.**
7. A liveness line on head-up even when the queue is empty, so "desk down" is
   distinguishable from "BLE dead".

Degradation is always **toward silence**, which is the correct direction.

---

## 3. Why two gates with two different mechanisms

The AUC gap is not the reason. The reason is **cost asymmetry**.

**G1 — noise vs queue-worthy, AUC 0.898** (zcode's correction; my own 0.958
was noise-vs-*interrupt*, the wrong question for suppression). A mistake here
costs one queue slot and is trivially undone. Volume is high, so a threshold can
be tuned. **This is the one place a model is allowed.**

**G2 — glance-worthy vs interrupt, AUC 0.813 zero-shot.** Subjective and
context-dependent; the clock-feature fix was tested and **falsified**, so the
residual error is tier assignment, not missing information. A mistake here costs
*trust*, which is abandonment risk #1.

**No zero-shot score meets the precision budget at usable recall.** Measured:
best precision 0.875 at recall ≥0.50, 0.688 at recall ≥0.90.

### The statistical trap — the sharpest finding of the whole exercise

To demonstrate 0.99 precision with 95% confidence and zero false pushes:

```
0.99^n ≤ 0.05   →   n ≥ 299 clean pushes   →   75 days at 4/day
```

Two clean weeks can only prove ≥0.948. **A learned gate can never be *shown* to
meet the target.** So the interrupt gate must be a whitelist of things true by
definition — rules, not scores. The learned per-wearer weights apply later, to
the *queue*, where the stakes are a queue slot.

Both reviewers spotted this independently, and both identified the same
consequential flaw in `documents/08`: its calibration math runs on **queued**
items, while the product's risk is on **pushes**. Different populations,
different base rates, different stakes. The `decisions` table needs the
population named explicitly.

---

## 4. G3 — glanceability, and why the decision model is not used there

Turn B measured winnow at AUC 0.838 against a 25-line regex's 0.688 — but:

- **winnow verifies *concreteness*, not *glanceability*.** All five false
  positives are buried-deadline items. The fact is present, just rendered in
  the last line. It checks "is the fact there", not "is it in the fovea".
- **Its confidence is unusable**: 0.54 on correct judgements vs 0.47 on incorrect.
- **It cannot be afforded inline**: it holds 95% of VRAM, so a generator cannot
  be resident alongside it. Inline it costs a model ping-pong, not 0.11 s.

So: **the regex blocks, winnow logs offline, batched.** Implemented in
`pipeline.py` as `check_glanceable()` / `repair_buried_fact()`, 11/11 cases
passing.

**On failure, repair — never suppress.** Deterministic reorder moves the
clause carrying the concrete token to the front; a vague summary gets one
stage-2 retry; drop only if both fail.

---

## 5. What gets cut

Agreed by both convergence rounds:

- **tev1** — verified: no chat template, rejected by the chat endpoint.
- **The clock/deadline feature** — falsified, zero precision gain.
- **S2 and S3 debounce** — S2 is S1 with a worse constant; S3 manufactures 31%
  re-examinations.
- **The refiner / free-text profile** — a model narrating 40 samples it cannot
  evaluate.
- **Voice** (`0xF1`/`0x0E`) — a second input path competing for the same
  attention budget.
- **Implicit-feedback learning** — 7-day half-life against 10–41 day
  convergence is *non-convergence*.
- **The known-bad code in `memory.py`**: the scalar `topic_weights` /
  `needs_detail` double-count, dead `star`, and the fictional
  `detail="touchbar/triple-tap"` provenance (0x04/0x05 are one alternating
  gesture and are deliberately unbound).
- **Email, Slack, CI feeds** — no OAuth, no integrations, until one source
  yields a fortnight of labels.
- **The two-stage pipeline for queue lines** — queue lines are template plus
  truncation; no model call.

---

## 6. Milestone 1 — "spool to lens"

**The outcome:** for 14 days, the lens is where I first learn a meeting is ten
minutes away or my blocked job is done, and nothing else ever interrupts me.

**Day 0, before anything else:** the ten-minute no-glance calibration walk. It
settles ρ, the BLE preflight, and MTU in one session.

```bash
python main.py --calibrate-walk --minutes 10
```

Then set `dwell = max(500, 1.5 × p99)` from the lengths of the `0x02 → 0x03`
events.

**Day 1–14:** one source (the agent spool), **queue only, no push**, nightly
stratified review under 60 s/day — stratum A uniform (the only stratum where a
false-positive rate is estimable), stratum B never-examined (21 of 30 queued
items were never examined in testing).

**Verdict at day 14:**

```sql
SELECT count(*)                                        AS shown,
       sum(label=1 AND already_knew=0)                 AS earned,
       sum(label=0 AND already_knew=0)                 AS false_alarm,
       sum(already_knew=1)                             AS redundant,
       sum(label IS NULL)                              AS unreviewed
  FROM decisions
 WHERE created_at BETWEEN :t0 AND :t0 + 14*86400;
```

**Continue if** earned ≥15, false_alarm ≤1, redundant < earned, unreviewed = 0,
and the glasses were worn ≥4h on at least 10 of 14 days.
**Stop if** they were worn fewer days than that, or redundant ≥ earned (the
laptop already does the job).

Not a platform. An outcome.

---

## 7. Contested — implemented as options, not verdicts

> **A disagreement between reviewers is a measurement to be taken, not a
> position to be defended.** Every question below is a declared axis in
> `policy.py`, with its deciding metric named *before* the data exists and a
> conservative default that applies until it does.

**Expand at setup, streamline at runtime.** At install time the axes are open
and calibrate against real measurements. At runtime there is no branching on
opinions: one resolved `Policy` object is read, and the hot path decides
nothing.

```bash
python main.py --policy-axes     # every axis, its options, its deciding metric
python main.py --policy          # current resolution and what is still open
python main.py --set-policy g1_model=on     # pin one by hand, persists
```

| axis | options | default | settled by |
|---|---|---|---|
| `examination_source` | `touchbar`/`head_up`/`both` | `touchbar` | ρ = θ/A_b from the calibration walk (≥1.6) |
| `g1_model` | `on`/`off` | `off` | `queue_worth_rate` over ≥100 review labels |
| `interrupt_gate` | `rules`/`learned` | `rules` | ≥299 clean pushes |
| `detail_generation` | `off`/`on` | `off` | `drill_miss_rate` over the first fortnight |
| `push_enabled` | `off`/`on` | `off` | a full fortnight of tier labels |

**Safety property, tested:** with zero evidence every axis holds its
conservative default. No axis can be switched on by opinion.

`detail_generation` is already wired into the live path — with the default,
a tap logs `drill_miss` and does not spin up a generation. Observations are
stored separately from reactions (`OBSERVATION_KINDS` in `memory.py`) because a
drill that found nothing says something about *how detail is generated*, not
about the subject, and mixing the two would corrupt the relevance estimate.

### The reasoning underneath each axis, for the record

**Does winnow survive at all?** (axis: `g1_model`) Claude cuts it *everywhere*, including the
offline judge, arguing 299 samples are needed and the VRAM ping-pong is not
worth it. zcode keeps it for G1 and as a batched offline judge, because G1
errors are cheap and 0.898 is adequate for a task whose failure mode is a lost
queue slot. **Unresolved.** Lean zcode on G1; the offline judge is genuinely
marginal and Claude's objection to it is fair.

**Is the examination signal usable at all?** (axis: `examination_source`) Both convergence reviewers
attacked Experiment C harder than C attacked itself. C's 275 ms maximum gait run
is circular — a simulated sine caps it by construction. More seriously, **a
dwell filter cannot distinguish looking up at the glasses from looking up at a
sign or a person.** So `examined` is an upper bound only, and contamination is
worse than C reported. Claude suggests that if p99 > 333 ms, only the TouchBar
opens the queue and head-up stops being an examination signal at all.

**Is the 0.813 boundary learnable at all?** (axis: `interrupt_gate`) Claude: the personal-calibration
framing is unproven — the alternative is that it is unlearnable noise. zcode
concedes this and says day-14 labels settle it; do not pre-commit a learner.

**Which of my own claims are still wrong?** Three corrections are already on the
record in `documents/README.md` (the 22 ms figure, the VRAM claim I
over-corrected, tev1). Expect more.

---

## 8. What is measured, and what is assumed

**Measured on this machine:** all BLE packet formats (31 offline checks); the
display budget in characters; winnow at 19.1 s cold / 110 ms sustained / 5826 MiB
resident; discrimination AUCs 0.898 and 0.813 and the falsified clock fix;
glanceability 0.838 vs 0.688 with paired bootstrap; gait debounce constants and
the ρ cliff; walking-tier CPU cost; the absence of BlueZ.

**Assumed, never tested:** everything about the physical G1 — that
`0x02`/`0x03` mean head up/down at all, that the display is 5×24, that MTU
negotiation behaves as described, that text written with 0x4E shows
regardless of head angle, and that firmware threshold θ exists as modelled.

**The number that gates the most: θ.** Nobody has measured it. Until the
calibration walk, every claim resting on the examination signal is conditional.