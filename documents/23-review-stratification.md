# 23 — Review stratification: what documents/08 claimed, and what the ledger can see

*Date: 2026-10-17. Follows [22](22-interruption-verification.md).*

---

## Why this was run

documents/22 closed the censoring question, and it closed the randomised audit
prompt by calling it a product decision — the wearer's, not this project's. That
leaves the oldest open question on the labelling track, and it is the one
documents/08 said was not optional:

> **The stratification is not optional.** Stratum A is uniform over everything
> observable (the only stratum where a false-positive rate is estimable); Stratum
> B deliberately over-samples never-examined items. In testing, **21 of 30
> queued items were never examined** — invisible mass that a reactions-only
> review would miss entirely.
> — [08](08-panel-learning-substrate.md) §2, lines 120–125

Seven months of commits later there is a ledger, an idempotent `record_decision`,
a `--review` that refuses to label a censored row, and `label_stats()` that
deliberately keeps queued labels away from `clean_pushes`. So: does documents/08's
claim still hold?

Part of it holds and part of it does not, and the part that does not is worth
more than the part that does.

## Method, and what bounds it

The four checks the brief asked for, run as given:

```
$ .venv/bin/python main.py --review --store $(mktemp -d)/r.db
Review — the only ground truth this system has.

  y  worth interrupting        n  not worth it
  k  worth it, but I already knew    s  skip
  q  stop

  nothing waiting. Nothing is ever dropped from the queue;
  this is empty because nothing has been judged yet.
```

```
$ .venv/bin/python experiments/learning_substrate.py
OK    no saturation, evidence gate holds, ranking is measured, silence stays neutral
```

```
$ .venv/bin/python experiments/interrupt_gate.py
12. the whole loop closes: decide -> record -> label -> evidence
  ok   CONTROL: with nothing labelled, the label metrics are ABSENT  [n_labels=None worth=None]
  ok   queue_worth_rate is the mean over QUEUED rows ONLY  [worth=0.0 from 2 queued rows; 2 dry-run pushes correctly contributed 0]
  ok   clean_pushes still ABSENT -- none of these pushes was the result of a real send  [None]
  ok   a push made with the link down is recorded unlabelable  [censoring applies to PUSHES, not to terminal review]
13. re-recording the same event does not manufacture ground truth
  ok   CONTROL: three replays of one spool still yield one row each  [3 -> 3 rows for 3 candidates]
  ok   a decision that CHANGES updates in place, not as a new row  [2 rows; row 2 is now ['push']]
OK    silent by default, nothing dropped, budget binds the batch, order is stable
```

```
$ .venv/bin/python main.py --evidence --store $(mktemp -d)/e.db
  metric              source        value
  ----------------------------------------------------
  pushes_today        derived       0
  pushes_last_hour    derived       0
  label_days          no source     —  <-- ABSENT, not zero
  clean_pushes        no source     —  <-- ABSENT, not zero
  n_labels            no source     —  <-- ABSENT, not zero
  queue_worth_rate    no source     —  <-- ABSENT, not zero
  rho                 no source: ne —  <-- ABSENT, not zero

  policy as resolved:
    ?? examination_source   touchbar   no calibration walk yet
    ?? g1_model             off        0 labels < 100: unproven on real data
    ?? interrupt_gate       rules      0 clean pushes; need 299 to show 0.99 at 95% confidence
    ?? detail_generation    off        no drill log yet
    ?? push_enabled         off        0 days of tier labels; milestone 1 is queue-only
```

`--review` on an empty store says nothing and exits 0, which is the correct
empty state and no evidence at all about ordering. So the population below was
built: a spool of 1,476 candidates over 60 days — 24 `digest` items a day plus
one `meeting`, one `blocked_job` and one `device` every fifth day — put through
the real dry run and recorded with the real `--queue-record` path:

```
spool files : 1476
  G2 column is shadow information, not the decision: 36/1476 would qualify on the deterministic rules alone.
  {'push': 0, 'queue': 1476, 'gate:budget/axis': 1476}
  recorded 1476 decisions to /tmp/strat/pop.db
```

**The population is a construction and its shape is illustrative.** Every number
below that depends on how many `digest` items a day a real wearer receives is
marked as such. Every number that depends on how the code behaves was measured
by running it. Nothing in the repository was modified; all scratch databases are
under `/tmp`.

## 1. What documents/08 claimed, precisely

Three claims, all in the design-time panel document:

1. **Stratum A** is uniform over everything observable — "the only stratum where
   a false-positive rate is estimable" (08:121).
2. **Stratum B** is a deliberate over-sample of never-examined items, measured
   at "21 of 30 queued items were never examined" (08:122–123).
3. The queries are written down, in the schema document, and are runnable
   ([09-schema.sql:162–175](09-schema.sql)): stratum A hashes `id` into a
   uniform draw; stratum B is `WHERE label IS NULL AND observable = 1 AND
   examined = 0`.

## 2. The stratum queries do not execute

The shipped `decisions` table is, in full, measured from a live store:

```
   id, created_at, decision, cls, source, line, gate, reason, observable,
   dry_run, dedup_key, label, already_know, reviewed_at
```

documents/09-schema.sql:56–58 declares `first_exam_at`, `last_exam_at` and
`examined`. None of the three exists. Running the two stratification queries from
documents/09 against the shipped store:

```
  stratum A (documents/09:163-168): sqlite3.OperationalError: no such column: candidate
  stratum B (documents/09:170-175): sqlite3.OperationalError: no such column: candidate
```

`candidate` and `outcome` are presentation columns that were renamed on the way
in. Delete them and the real reason appears:

```
  stratum B minus candidate/outcome: sqlite3.OperationalError: no such column: examined
```

**So documents/08's central claim does not hold as written, and the failure is
not that the estimate was wrong — it is that the quantity is not in the data
model.** The never-examined mass cannot be sampled, over-sampled, or even
counted, because nothing records it. `note_glance()`, which documents/08 §3
proposes wiring into the `0xF5` decode at line 136, does not exist anywhere in
the repository; `note_unexamined()` does exist (`memory.py:1188`) and its only
caller is `experiments/suspend_gate.py`, as `experiments/wiring_audit.py:198-212`
already says.

There is a column that looks like the one wanted, and it measures the opposite
condition. `observable` records whether the glasses were on the wearer's face
when the card was decided (`main.py:850`, `session.py:451-455`). `observable = 0`
means the glasses were **in a case** — that is documents/22's censoring flag, and
it says the row is *unlabelable*. It is not "the wearer looked away". documents/21
§"The observability cell" predicted exactly this trap: `observable` is a
prerequisite for stratum B, not stratum B.

documents/08 §3's own caveat survives untouched and is worth restating because it
is the reason nothing was built on `examined`: `0x02`/`0x03` may be the firmware
dashboard gesture rather than an examination, "treat `examined` as unproven until
real wear data settles it" (08:169-173). documents/21 then ruled head-up
unusable as a label at runtime, gated on a ρ ≥ 1.6 calibration walk that needs a
real device. `--evidence` above still shows `examination_source` unresolved with
`rho` marked `no source: ne…`.

## 3. There is a real never-reviewed mass. It is not the one documents/08 named.

The intuition was right and the mechanism was wrong. Measured on the 1,476-row
population:

| quantity | value |
|---|---|
| rows in the ledger | 1,476 |
| unlabelled rows inside `unlabelled()`'s 30-day horizon | 738 |
| unlabelled rows **outside** it, and therefore unreachable | **738 (50.0%)** |
| oldest unreachable row | 2026-08-19 |
| default review slice | 20 rows = **1.36%** of the ledger |
| classes in that slice | `['digest']` |
| span of that slice | 08:00 → 08:50, one morning |
| oldest-first depth at which `meeting` first appears | row 25 |
| … `blocked_job` | row 74 |
| … `device` | row 123 |

`unlabelled()` takes `since_days: float = 30.0` (`memory.py:1334`) and
`main.py:589` never passes it, so the horizon is fixed at thirty days and rows do
not come back. Half the ledger is permanently invisible, it is not counted
anywhere, and nothing prints a word about it — the review reports
"{n} to review (oldest first…)" (`main.py:602-604`) and the shortfall is
invisible from inside.

Two things are true at once here and both are worth saying. "Oldest first" is
still the right order (`memory.py:1339-1342` gives the reason: a newest-first
queue drifts toward whatever happened today). And "oldest first" is currently a
selection on **nothing but row age**, which on this population means a reviewer
spends a whole session inside one class that can never interrupt — 20 of 20 rows
in the first slice, and the first interruptible item does not appear until row 25.

## 4. The finding that is worse than the one documents/08 was looking for

**The review order is already an input to a policy axis, and the axis it feeds is
`push_enabled` — the one that decides whether anything ever reaches a face.**

Same ledger, same 100 labels of human effort, same ground truth, two allocation
schemes, both inside the shipped 30-day horizon:

| allocation | labels collected | `label_days` | `queue_worth_rate` | `push_enabled` |
|---|---|---|---|---|
| oldest-first (shipped) | 100 | 5 | 0.020 | **off** — "5 days of tier labels; milestone 1 is queue-only" |
| quota, 33 per class | 51 | 19 | 0.353 | **on** — "19 days of tier labels collected", `enough_evidence=True` |

Three separate defects line up to produce this:

1. **`label_days` is distinct days of *any* labelled row** (`memory.py:1398-1401`).
   `push_enabled`'s deciding metric is named "tier labels collected for a full
   fortnight" (`policy.py:160`) and there is no tier column anywhere. The axis is
   satisfied by days on which a human looked at *anything*, including the class
   documents/13 and documents/21 agree can never interrupt. A quota scheme that
   touches 19 different days on 51 labels satisfies it; oldest-first spends 100
   labels to touch 5, because 100 consecutive rows by age are five consecutive
   mornings and nothing else.
2. **`n_labels` and `queue_worth_rate` are computed over different populations.**
   `n_labels` counts every labelled row (`memory.py:1395-1397`); `queue_worth_rate`
   is the mean over `decision = 'queue'` only (`memory.py:1380-1383`). Measured
   with 120 labels all on pushed rows: `n_labels=120`, `queue_worth_rate=None`,
   and `g1_model` resolves *"no queue_worth_rate yet"* — the volume gate passes
   on labels the rate cannot use. It is not a bug in itself; the comment at
   `memory.py:1390-1394` is explicit that dry-run labels are real content
   evidence. But it means the two numbers read as one number and are not.
3. **`clean_pushes` is ordered by `reviewed_at, id`** (`memory.py:1388`) — review
   order, not push order. Six pushes, identical labels, only the review order
   differs:

```
  pushes 0-5, correct = {2,3,4,5}, reviewed oldest-first  -> clean_pushes=4
  pushes 0-5, correct = {2,3,4,5}, reviewed newest-first  -> clean_pushes=0
  pushes 0-5, correct = {0,1},    reviewed oldest-first  -> clean_pushes=0
  pushes 0-5, correct = {0,1},    reviewed newest-first  -> clean_pushes=2
```

   `--review push` is oldest-first, so the intended semantic — a streak since the
   last false push — holds while the review order equals the push order and stops
   holding the moment it does not. Nothing records that the two orders are
   supposed to be the same.

And the column that would make a principled correction possible is empty of
information. `plan()` checks `push_enabled` before G2 (`interrupt.py:433-437`), so
with the default policy every row short-circuits on the axis:

```
  distinct (gate, reason) pairs in the ledger   : 1
  distinct gate values                          : ['budget/axis']
  rows whose reason mentions 'eligibility'      : 0
```

**Every row in the ledger is the same row as far as the gate is concerned.** The
36 items that would have interrupted on the deterministic rules are stored
identically to the 1,440 that could never, and `main.py:816` computes the "would
qualify" column for the terminal without ever writing it down.

## 5. The traps, with numbers

Everything in this section is measured on the population above, so the *rates*
are illustrative. The *behaviour* is not.

**The easy-case trap.** documents/08 was right about the mechanism and could not
name it in the data. Measured coverage of the only interruptible class:

| allocation | n | unweighted worth | 95% CI | post-stratified | interruptible items touched |
|---|---|---|---|---|---|
| oldest-first 20 (shipped) | 20 | 0.000 | [0.000, 0.161] | 0.0000 | **0 / 36** |
| newest-first 20 | 20 | 0.050 | [0.009, 0.236] | 0.0083 | 1 / 36 |
| systematic every 37th | 20 | 0.000 | [0.000, 0.161] | 0.0000 | 0 / 36 |
| quota 5/5/5 + 5 digest | 20 | **0.750** | [0.531, 0.888] | 0.0244 | 15 / 36 |
| quota 15 digest + 5/5/5 | 30 | **0.500** | [0.332, 0.668] | 0.0244 | 15 / 36 |
| true rate over the reachable population | | **0.0244** | | | |

Both failure directions are live and they are symmetric. Oldest-first biases
`queue_worth_rate` toward zero on a population that is 97.6% the class which can
never interrupt, which resolves `g1_model` to `off` **with
`enough_evidence=True`** — the project concludes "filtering the queue was
pointless" from a sample that never looked at the interruptible class. A quota
scheme biases it by 20×, and the quota-15 row lands on exactly 0.500, which is
`g1_model`'s threshold (`policy.py:227-230`). The post-stratified column — the
same sample weighted back to the population it came from — recovers 0.0244
exactly, which is the whole argument for weighting: the unweighted mean over a
deliberately non-representative sample answers a question nobody asked.

**`n_labels` is a volume gate, so every scheme distorts it.** `g1_model` wants
`n_labels >= 100` (`policy.py:74`, `policy.py:220`). A quota scheme spends its
effort where the population is thin and can return fewer labels from the same
budget — 51 instead of 100 above, because the interruptible classes ran out at 12
rows each. The scheme that knows the most about the queue collects less of what
the axis counts.

**Censoring is structurally absent from the ledger today.** `main.py:850` records
`observable=True` for every dry-run row, on the stated grounds that a human
reading text on a terminal does not need the glasses on their face. Measured:
`observable=1` on 1,476 of 1,476 rows. The censoring machinery is correct and is
exercised by `experiments/interrupt_gate.py` §12, but in a live phase it fires
only on real pushes made while the link was down — and `record_verdict()` is
called from exactly one shipped place (`main.py:850`), which passes `dry_run=True`
and `observable=True` for everything. Until a push path exists, the ledger has no
censoring in it.

**`expand` cannot reach `label`, and this was verified rather than assumed.**
`--review` accepts `y`, `n`, `k`, `s`, `q` and nothing else
(`main.py:641-659`); `k` writes `set_label(1, already_knew=True)`, which is a
value label plus a separate column, not the legibility signal documents/22 §2
forbids. There is no code path from a TouchBar reaction into `label`.

## 6. The deciding axis, proposed

Not added to `POLICY_AXES` — that is a code change and this is a document. Named
here so it is argued about rather than implemented by accident:

```
review_allocation
  question : Which rows does --review put in front of a human?
  options  : oldest_first | systematic | class_stratified
  default  : systematic  (conservative until proven)
  decided by: the gap between the unweighted queue_worth_rate and its
              post-stratified reweight, over >=200 labels
  from     : documents/08 §2 named the strata; documents/23 measured what
              oldest-first and quota each do to a resolved axis
```

**Conservative default: `systematic`.** The argument is not that it is more
accurate — on this population it is indistinguishable from oldest-first (both
0.000). It is that it is the only option whose estimate is unbiased *by
construction*, so it cannot be argued into turning an axis on or off. The
alternative conservative reading — keep `oldest_first`, because it is the status
quo and it happens not to trip `push_enabled` — is worse: it is conservative by
accident, and the accident is 50 minutes of `digest`.

**`class_stratified` is the audit lane, not the default**, and the rule attached
to it is the one that matters: a stratified sample's estimate is reported
**post-stratified**, and the raw mean of an oversampled sample is never fed to
`resolve()`. Under that rule the quota row above reports 0.0244 rather than
0.750, and the g1_model threshold question stops being decided by the allocation.

Two conditions the proposal does not satisfy, stated so they are not quietly
assumed:

- **The horizon is upstream of every scheme.** `unlabelled()` cuts at 30 days
  before any picker runs (`memory.py:1344`), so no allocation can reach the 738
  aged-out rows. A scheme that claims to cover the never-reviewed mass while
  `since_days` is 30 is describing half the mass and calling it all.
- **`push_enabled` must stop reading `label_days` for tier labels.** Until it has
  a tier column, the review order is load-bearing for it, and the measurement in
  §4 is the proof. This is a code change in `interrupt.py:547` and it is not
  proposed here.

## 7. What cannot be done, and why

- **Any examination-based stratification.** Needs `examined`, which needs the
  ρ ≥ 1.6 calibration walk on real hardware, which needs a G1 and a wearer.
  documents/21 puts that at rung 4 of the ladder and `g1_driver` has still never
  touched a device. Until then documents/08 §3's own caveat governs: `examined` is
  unproven, so a stratum defined on it would be a stratum defined on a guess.
- **Inverse-propensity reweighting from the ledger.** Not merely unvalidated —
  inoperable. IPS needs a per-row probability of having been shown the item, and
  the treatment indicator is constant: 1,476 of 1,476 rows read `gate=budget/axis`
  with one distinct `(gate, reason)` pair. There is nothing to weight by.
- **Estimating what a pushed item would have been worth, from queue labels.**
  documents/22 §"The structural conflict" is the ban, and §4 above is a concrete
  instance of it: `queue_worth_rate` is computed over rows selected by the very
  axis that would have pushed them, so it estimates P(worth | the gate said no),
  which is a statement about the axis. That is the one thing `g1_model` is
  allowed to decide on, so the axis is justified using a number measured on its
  own decisions.
- **Settling the 0.5 threshold from review labels.** On this population the best
  achievable `queue_worth_rate` at `n_labels >= 100` is 36/100 = 0.360, taking
  *every* interruptible row that exists. The `on` branch (`policy.py:227`) needs
  ≥ 0.50, i.e. more interruptible items in the sample than everything else
  combined — which stops being an estimate of the queue. Whether 0.5 is the right
  cut is a question about interruption cost, and documents/22 found no evidence
  for any numeric budget.
- **The randomised audit prompt on the lenses.** documents/22 declined it as a
  product decision. It is still one, and this document does not make it.

## 8. Claims falsified, kept visible

| claim | where | what happened |
|---|---|---|
| "The stratification is not optional… stratum B deliberately over-samples never-examined items" | 08:120-125 | The quantity is not in the shipped schema. `examined` does not exist; both documents/09 stratum queries raise `OperationalError`. |
| "21 of 30 queued items were never examined"; "Cross-tab over 630 decisions" | 08:123, 08:157-167 | Unreproducible and unreproducible-by-construction. Both numbers are counted against an `examined` column that was never shipped. Not retracted as wrong; withdrawn as unmeasurable. |
| "j/k to bulk-apply a run of obviously-bad items" | 08:117 | Never shipped, and `k` now means something else — "worth it, but I already knew" (`main.py:593`, `main.py:651-653`). Anyone following documents/08 at the keyboard writes a *positive* label where they meant to skip a run. |
| "python main.py --calibrate --days 14 --fp-cost 5 [--apply]", "`--min-labeled 100`" | 08:211, 08:245 | No such flags. `--policy-axes` lists the whole surface and calibration is not on it. The log-odds feature machinery of 08 §4 (`features`, `decision_features`, `feature_counts`) is not in the shipped schema either, which is why `rho` reads `no source` in `--evidence` above. |
| `self.store.note_glance(data[1])` at the `0xF5` hook | 08:136 | Does not exist. `note_unexamined()` exists, is dead outside `experiments/suspend_gate.py`, and is what `wiring_audit.py:198-212` already says about it. |
| "Stratum A is uniform over everything observable (the only stratum where a false-positive rate is estimable)" | 08:121 | Still true and still the only stratum that is. What changed is that the uniform draw is now the *whole* problem rather than half of it, because §4 shows the draw's order moves an axis verdict. |

## 9. Carried forward, unverified

- **`documents/08` links `Schema: [08-schema.sql](08-schema.sql)`.** That file is
  not in the repository (`documents/` holds `09-schema.sql` only). The schema the
  stratum queries were validated against is not available to re-check.
- **The `record_decision` staleness claim in this section was itself stale, and is
  resolved.** The first draft of this bullet read "`experiments/wiring_audit.py:213-220`
  is stale about `record_decision`… missed by the commit that made it wrong." That was
  committed **26 seconds after** `b7c3fc9` had already fixed it: the entry is gone, and
  the audit now FAILS on an acknowledgement whose function a shipped module calls. Kept
  here because a document that silently drops a wrong claim is how the next reader
  re-derives it. The attribution was wrong in a second way too — the shipped call site
  is `interrupt.py:764`, where `record_verdict()` calls `store.record_decision()`,
  reached from `main.py:867` under `--queue-record`. Not `main.py:836-851`, which is a
  `print()` inside the dry-run report and contains no wiring at all.
- **A third rottability class, found while fixing the above: an acknowledgement that
  blames a credential.** `ambiguous_verdict()`'s entry claimed the function was
  unreachable because `jev_gate.py` could not authenticate, with the revival condition
  "the caller exists; only the credential is missing". Both halves were false —
  `jev_gate.py` has never referenced that function in any commit, so the key was never
  the blocker. The entry is now a statement about the code (`NO CALLERS AT ALL`), and
  the audit fails on any reason or revival condition that blames a credential, because
  such a claim is unverifiable from inside the repo: a key lives in a gitignored `.env`,
  so the entry turns false the moment someone configures it and nothing in the diff
  shows it happening.
- **The 120-character display is nowhere in this document's arithmetic**, because
  nothing here touches legibility. The 5×24 grid and the 0x25 heartbeat's ~32s
  budget constrain the *content* of a label, not which rows get shown. A review
  queue that grows longer than the display can hold is a different failure and is
  not measured here.
- **Every population figure above is from a synthetic spool.** The mechanism
  results — which order reaches which class, which allocation moves which axis,
  which column is missing — are properties of the code and hold regardless. The
  rates do not.
- **`expand` reaching `label` is verified for the current `--review` only.** A
  future audit prompt on the lenses (documents/22 ranked it second) would be a
  new path into the same column, and the constraint in `memory.py:57-68` would
  then be a comment rather than a guarantee.