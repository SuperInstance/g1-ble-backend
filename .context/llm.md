# Sector digest: `llm`

> Written by an independent reviewer reading the code, not by the
> agent that wrote it. Point-in-time: 2026-10-16.

## llm  (pipeline.py, memory.py; evidence: documents/10-turn-B-glanceability.md, experiments/results/REPORT-A2-ordinal.md)

Read on 2026-10-16. **No code was run for this digest.** Items marked [static]
come from reading the code; re-check them with the commands below.

### State
- The pipeline is `run_query` (pipeline.py:880):
  - stage 1 writes the full answer (:733, 1200 tok);
  - stage 2 compresses it (:791, 120 tok, T=0.2);
  - then `enforce_display_budget` (:412), the gate `check_glanceable` (:589)
    and the reorder `repair_buried_fact` (:631);
  - `send()` always runs. Fallbacks: truncated stage 1, or a static
    "LLM error." line (:933-948).
- The display budget is 120 chars (5×24) and 25 words, whichever binds first
  (:40-42). The 120 comes from the demo-app layout and was never measured on G1
  hardware (no BLE path on WSL2).
- The gate repairs and never suppresses. If the repair still fails the gate,
  the reordered text ships anyway with only a log warning (:852-856).
  - Not implemented from Turn B's verdict: one stage-2 retry with feedback
    (§6.3), SMIF-sentence extraction, and "suppress if both fail".
- The winnow model is not on the display path. It only runs offline in
  experiments/score_corpus.py and clock_feature.py. The "offline logging judge"
  (§6.2) is not built.
- memory.py is an append-only SQLite store
  (~/.local/share/g1-ble-backend/feedback.db).
  - Reaction weights at memory.py:45-50; 7-day half-life; weights clamped to [0,3].
  - `preferences()` (:403) goes into BOTH the stage-1 primary prompt
    (pipeline.py:759) and the stage-2 prompt (:814), although the docstring
    says stage 2 only.
  - Callers: session.py:147/184/232, plugins.py:272, main.py:340.
- Observation kinds `repaired`, `shown` and `suppressed` (memory.py:57-62) are
  declared but never written. Only `drill_miss` is (session.py:222).
  pipeline.py does not import memory.

### Measured vs assumed
| claim | status | source |
|---|---|---|
| regex AUC 0.688; winnow src+summ 0.838; ΔAUC +0.150 CI [+0.036,+0.285] | measured, n=50, author-built labels, prompt tuned on 4 dev items | Turn B §3 |
| winnow blind to fact burial (all 5 buried items false positive, mean 1.68) | measured | Turn B §3 |
| winnow 0.110 s warm, 0.5 s from idle, 3.55 s cold; VRAM 5.8/6.1 GiB, can't co-reside with qwen (4.0-4.7 GB) | measured | Turn B §4 |
| ~7 s per two-stage summary on qwen2.5-coder:7b | measured, 10 queries | Turn B §1 |
| winnow confidence uncalibrated (0.54 when correct vs 0.47 when wrong) | measured | Turn B §3 |
| salience AUC: noise/interrupt 0.958, glance/interrupt 0.813, Spearman 0.737 | measured, n=40 (12 pos); 1 error = 8.3 precision pts | REPORT-A2 |
| a clock/deadline feature improves precision | **falsified** (+0.000; veto variants worse) | REPORT-A2 |
| the SHIPPED `check_glanceable` performs like 0.688 | **assumed**: it adds referent, hedge and lead≥6 rules (:609-626) never measured | pipeline.py:589 |
| the fact must sit in the first ~48 chars to be glanceable | **assumed** | Turn B §1 |
| injecting interests improves answers | **FALSIFIED** (2026-10-16). The A/B ran: the salience gate fires on **0/8** ordinary questions, and when it fires (n=3) it shifts answer polarity and destabilises it — which rule 9 forbids. Costs 0.44s vs 0.25s. | experiments/personalisation_ab.py, documents/18 |
| EVENT_WEIGHTS, HALF_LIFE_DAYS=7, GLANCEABILITY_MIN=0.5 | **assumed**, untuned | memory.py:54,75; pipeline.py |

### Open
- **Does the shipped gate beat 0.688, and how often does it fire on real
  stage-2 output?** Settled by AUC and fire-rate on a held-out set committed to
  the repo. Turn B's 50-item set lives only in /tmp
  (experiments/glance_gate.py:6-9).
- **Does the repair help?** Settled by the share of gate failures with the
  SMIF in the first 48 chars after vs before repair, and the share that still
  fails but ships (:856).
- **Are 120 and 48 chars the right budgets?** Settled by on-device
  re-examination rate by fact position. Blocked on BLE.
- **Is the glance/interrupt boundary (0.813) learnable per wearer?** Settled by
  precision at fixed recall after N wearer labels.
- **Do gate outcomes reach the ledger?** This is a prerequisite for all
  calibration. Settled when the count of `repaired`/`shown` events is above 0.

### Traps
1. [static] Lexicon words in `_CONCRETE_TOKEN_RE` lack `\b`, so "know" matches
   "now" and "required" matches "red". The `lead` and repair scores are
   computed on substrings (pipeline.py:529-531).
2. [static] The clause split `\s+[-,]\s+` needs a space before the comma, so
   "x, y" never splits and the repair silently does nothing (pipeline.py:644).
3. [static] Kept text plus "…" can reach 121 chars, one char onto a 6th line.
   The repair re-budgets with module defaults, not config (pipeline.py:453-455,
   :674). The prompt's "aim 20-25 words" (:347) is about 114-140 chars, so
   cutting off the end of the text is routine.

### Verify before changing
python3 experiments/glance_gate.py   # exit 0 == passed
python3 -c "import pipeline as p; print(p._CONCRETE_TOKEN_RE.search('I know').group(0))"  # 'now' => trap 1
python3 -c "import re; print(re.split(r'(?<=[.!?;])\s+|\s+[-,]\s+','fine, expires in 2 hours'))"  # 1 element => trap 2
python3 -c "import pipeline as p; print(len(p.enforce_display_budget('abcdefghij '*30)))"  # >120 => trap 3
python3 experiments/clock_feature.py  # re-derives the REPORT-A2 clock table
grep -rnE "add_event\(" *.py          # which observation kinds are written

There is no tests/ directory. glance_gate.py is the only executable check, and
its 11 cases were written by the code's author.

### Falsifiers
- On a repo-held set of production stage-2 text, the regex ties or beats
  winnow. Then "model > regex" is gone, and only burial-blindness and VRAM cost
  remain as findings.
- Logged repairs show no gain in SMIF-in-first-48-chars, or the
  ships-while-failing path (:856) dominates. Then "repair, don't suppress" is
  unsupported as built.
- On device, re-examination rate doesn't depend on where the fact sits. Then
  the 48-char premise falls, and `repair_buried_fact` with it.
- After N wearer labels, glance/interrupt precision stays at the zero-shot
  level. Then personal calibration is not the fix for REPORT-A2's split gate.
- An A/B with `--no-personalise` shows the same expand/dismiss rates. Then the
  memory.py injection is decoration. **Runnable today**: the flag is wired from
  the CLI through `Session` into `run_query`/`condense_for_glasses`, and the
  resulting stage-2 prompt is asserted byte-identical to the base prompt. What
  is still missing is a run with real wearer labels.
- **Stage 1 still consumes `preferences()`.** Stage 2 was moved to the gated
  `WearerContext`; stage 1 still receives the unguarded imperative string. That
  is the worse hijack of the two — it shapes what gets reasoned about rather
  than only what gets compressed — and it is now the main remaining exposure.
- Interest matching wins ties rather than leading. Stage 2 cannot log which
  fact it chose, so compliance with rules 8-12 is **unobservable**. Anti-echo
  (`already_knew=1`) can only be measured upstream; if it must live upstream,
  interests should live there too.
