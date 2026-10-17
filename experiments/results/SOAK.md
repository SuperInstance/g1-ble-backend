# Soak: 30 days of simulated wear, policy vs. fixed

Run: `.venv/bin/python experiments/soak.py --days 30`. Three seeds. Full output
in `soak.json`.

> ## CORRECTION — the headline `queue_worth_rate` below is FALSIFIED
>
> The 0.004–0.006 figure was produced by **a bug in the validation harness, not
> by the system under test.** It is kept in this document rather than deleted,
> because the error and its cause are the more useful record.
>
> **Cause.** `_settle()` charged queue items against *trust*. The harness's own
> comment on that code now reads: *"a queue item I ignored is not a mis-fire …
> Charge trust for queue noise and the wearer abandons on day one."* So the
> wearer abandoned almost immediately, abandoned items all scored 0, and
> `earned` stayed near zero — which the report then divided by `queued`. The
> simulation was measuring its own bookkeeping bug.
>
> **Second, independent defect in the same code path.** `glance_worth()` returns
> an **ordinal** 0/1/2, but `decisions.label` is constrained to
> `CHECK (label IN (0,1))`. The first item scoring 2 raised
> `IntegrityError: CHECK constraint failed` and **crashed the run on day 1,
> item 19**. So the soak could not complete at all; any long-run number that
> exists came from a run that stopped early. The ledger is now fed
> `1 if worth >= 1 else 0`, and urgency is counted separately.
>
> **Third.** `earned` counted only `worth == 1`, so urgent items (2) fell into
> neither earned nor rejected while still inflating the denominator.
>
> **Measured after the fixes** — 30 days, seed 7: `earned 538, rejected 271,
> queue_worth_rate 0.8164`. 14 days, seed 20261016: `0.71`. Reproducible across
> both seeds and both arms.
>
> **What survives.** The policy-vs-fixed conclusion below is *unchanged and
> re-confirmed*: identical outcomes, 0 axes ever flipping, 2/5 resolved. And the
> **arrival-rate diagnosis stands** — ~24 candidates/day into a 12-item queue is
> still a volume problem. What is falsified is the claim that the design
> "produces two orders of magnitude less worth than its own bar." It does not.
> It clears the bar, and it does so while still being unable to drain its queue.

## Setup

The virtual wearer was written by an independent reviewer explicitly told to
be **adversarial to the design**, so that the experiment could not be rigged in
the author's favour. Events come from `sim/events.py`: nine classes, realistic
hour-of-day weighting, ~24 candidates per day.

## Result (SUPERSEDED — see correction above)

| | seed 11 | seed 20261016 | seed 777 |
|---|---|---|---|
| candidates | ~750 | ~750 | ~750 |
| **overflow (evicted unseen)** | 736 | 678 | 676 |
| examined | 3 | 4 | 1 |
| earned | 4 | 4 | 3 |
| **queue_worth_rate** | 0.0053 | 0.0058 | 0.0044 |
| axes that ever flipped | 0 | 0 | 0 |

`DESIGN.md`'s milestone-1 continuation bar is `queue_worth_rate >= 0.5`.
The run then reported **0.004–0.006**. **That number was wrong** — harness bug,
plus a run that could not complete past day 1.

## The policy layer changed nothing

Across 30 days and three seeds, every metric is identical between arms:

```
  earned               policy=4   fixed=4   delta=0
  rejected             policy=868 fixed=868 delta=0
  policy axes resolved from evidence: 2/5
  fixed  axes ever move off defaults : 0/5
```

Read honestly: **the policy layer is not demonstrably earning its keep.** What
it does earn is a recorded reason for every default ("0 labels < 100: unproven
on real data"), which is worth something — but it has not changed an outcome.

This is a *mechanism* result, not a verdict on the idea. The wearer is a
simulation; it cannot say whether the product is worth wearing.

## Where the real problem is: arrival rate

Forcing `g1_model` on does not rescue it:

| variant | suppressed | queued | overflow | worth/queued |
|---|---|---|---|---|
| filter OFF | 0 | 694 | 678 | 0.0058 |
| filter ON | 261 | 433 | 416 | **0.0115** |

The filter doubles the worth rate and still leaves **96% overflow**. The
bottleneck is not noise, it is **volume**: ~24 candidates a day arriving into a
queue capped at 12, which a wearer who can afford ~4 peeks a day cannot drain.

> Re-measured after the harness fixes, the "worth rate" column in this table is
> meaningless — the 0.0058/0.0115 values came from the same broken accounting.
> The **overflow column is not**: 96% is volume, not bookkeeping, and it is the
> finding that still stands.

### A deadlock, not a filter problem

The queue only works if the filter runs *before* it fills. The filter is off
until the queue proves itself. The queue cannot prove itself because it
overflows. Neither arm can escape, and `push_enabled` cannot activate within 14
days because the policy can only see the previous day's labels.

## What this suggests

**Move the filter upstream to the writer.** The interaction review scored the
agent spool as "effort 1, value 9" precisely because the writer is the only
place with enough context to decide. Ingest 24 candidates a day and triage
downstream is the wrong shape; agents should write ~2 things a day, not 24,
and the queue becomes a delivery detail rather than a filtering problem.

## Caveats

- The wearer's cost curve may be harsher than a real person's. It was built to
  be, and I cannot tune it upward without rigging the test.
- `glance_worth` is scored on template-only queue lines, which is the design —
  but that means the design is being judged on its weakest possible text.
- 30 days is not the 299 clean pushes `interrupt_gate` needs. That axis cannot
  be tested in simulation at all.
