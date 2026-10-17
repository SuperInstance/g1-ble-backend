**The answer is not saved to any file.** Writing to `/tmp/round2-claude.md` was blocked because a file already exists there that I wasn't allowed to read. Writes to `/tmp/round2-claude-converge.md` and `documents/13-round2-claude.md` were refused too. Grant write access to one of those paths and I'll save it. The full answer is below.

---

# Round 2 — one design

**Thesis.** No scorer sits on any path that decides what reaches the lens. A fixed list of rules decides pushes. Push text comes from templates. Models run only at the desk, and only fill database columns that are allowed to be empty. The daily review is the only source of truth.

## 1. Event → lens, or nothing

| # | Gate | Features | Where | Cost |
|---|---|---|---|---|
| E0 | Read the event and check its format | files in `~/.g1/outbox/*.json`; must have `{source, class, line, ts}` | desk → Pi SQLite (at boot, then every 5 min) | µs; a malformed event becomes a `malformed` row |
| E1 | Route by type | `class` ∈ {`meeting`, `blocked_job`, `device`} may push; everything else goes to the queue | Pi | one lookup |
| E2 | Push condition per type | **meeting:** accepted, and 9–10 min before start. **blocked_job:** `blocking=true` and `elapsed_s > 120`. **device:** battery drops below 10% | Pi | µs |
| E3 | Budget | `pushes_60m < 2`, `pushes_today < 4`, type not suspended | Pi | one indexed `COUNT` |
| E4 | Build the text | **push:** template only, e.g. `Dentist 14:30 · 6 min`. **queue:** the source's `line` → regex check → reorder → fallback | Pi | µs |
| E5 | Log it | `decisions` row with the features frozen; `model_score` left empty | Pi | 0.75 µs |

- An item that fails E2 or E3 goes to the queue; nothing is dropped.
- The queue is opened by a TouchBar tap. Head-up opens it too, but only if the install walk clears it.
- Head-up debounce: 500 ms dwell, 400 ms release.
- One `n` in review suspends that push type for 24 h.

## 2. Are two gates justified?

Yes, but because the two kinds of mistake cost different amounts and get very different amounts of evidence. The AUC gap is not the reason.

- **Noise vs. anything (AUC 0.958).** A mistake costs one queue slot and is easy to undo. Volume is high, so the threshold can be tuned. This is the one place winnow belongs: a desk batch job that ranks the queue. It waits for milestone 2, because milestone 1 has no noisy source to filter.
- **Glance vs. interrupt (AUC 0.813).** You can't even check 0.99 precision at this volume. To show it with 95% confidence and zero false pushes, you need `0.99^n ≤ 0.05`, so n ≥ 299 clean pushes: 75 days at 4/day. Two clean weeks only prove ≥ 0.948. A learned gate can never be shown to meet the target, so the interrupt gate is a whitelist of things that are true by definition. The 0.813 score is logged, not used to decide.
- **Glanceability.** Winnow is cut: it checks concreteness, doesn't fit in VRAM alongside the generator, and its confidence is noise. The regex plus reorder stays, for queue lines only.
- **Head-up as "examined".** It feeds only the `examined`/`observable` columns and the choice of what opens the queue. It never becomes a label.

## 3. Minimum with no desk link (on the Pi)

1. BLE to both arms, MTU above 23, 10 s heartbeat, BlueZ autosuspend off.
2. Meeting pushes 10 minutes ahead, from a 24-hour calendar copy taken at boot.
3. Battery pushes.
4. The queue view, plus a status line: `desk 2h ago · 3 queued`.
5. Head-up and display logging.

Before day 0: a 10-minute walk without looking up, then set `dwell = max(500, 1.5·p99)` from the lengths of the up→down events. If p99 is over 333 ms, only the TouchBar opens the queue.

## 4. Cut

- winnow anywhere on the display path, and as an offline judge.
- qwen for display text, and on-demand item text (about 7 s each). DETAIL goes too.
- `tev1`, the two-stage pipeline, and the clock feature.
- `topic_weights`, keywords, the 7-day half-life, `needs_detail`, `RefinementPlugin`, `star`.
- The Beta scorer, until there are 220 or more labels.
- Re-reading (head-up twice) as a quality signal.
- S2/S3 debounce, the 300 ms dwell, and the 500 ms latency budget.
- Email, Slack and CI feeds, voice, and the E-C Bandit.

## 5. Milestone

**"For 14 days, the lens is where I first learn a meeting is 10 minutes away or my blocked job is done, and nothing else interrupts me."**

Review keys: `y` (worth it), `n` (shouldn't have fired), `k` (worth it, but I already knew). `k` needs one new column, `already_knew`.

```sql
SELECT COUNT(*) pushes,
       SUM(label=1 AND already_knew=0) earned,
       SUM(label=0 AND already_knew=0) false_push,
       SUM(already_knew=1) redundant,
       SUM(label IS NULL) unreviewed
FROM decisions
WHERE decision='relay' AND created_at BETWEEN :t0 AND :t0+14*86400;
```

- **Continue if** `earned ≥ 15`, `false_push ≤ 1`, `redundant < earned`, `unreviewed = 0`, and the glasses were worn 4 h or more on at least 10 days.
- **Stop if** they were worn on fewer days than that, or if `redundant ≥ earned` (the laptop already does the job).

## 6. Disagreements

- **Weakest claim, Experiment A2:** "0.99 precision is a personal-calibration problem." Push volume makes it impossible to answer. The labels doc 08 counts are on queued items, not pushes, so they would tune the boundary that doesn't need it.
- **Experiment C:** the 275 ms maximum gait run is close to circular, because the simulated sine waves cap it by construction. More importantly, a dwell can't filter out looking up at signs or people. So `examined` is only an upper bound, and contamination is worse than C says.
- **Experiment B:** checking text after it's written is weaker than requiring it at the source. Its repair step (a qwen retry) brings back the VRAM swapping it measured itself.
- **Doc 07:** putting the model on item/detail views puts the slowest part on the interaction that gets abandoned when it's slow.
