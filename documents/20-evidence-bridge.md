# 20 — The evidence bridge, and a metric that measured itself

*Date: 2026-10-16. Follows [19](19-wiring-audit.md).*

---

## The blocker

documents/19 closed with this as the next real increment:

> nothing builds a `Budget` from `FeedbackStore`, so no axis can ever leave
> its default

It is worth stating what that meant in practice. `policy.py` defines five
contested axes, each naming the metric that would settle it. **No shipped code
computed any of them.** So every axis resolved to its conservative default,
forever, and `Session` read the result on every drill-down. The policy layer was
a well-built instrument with no input.

There were two things to do, and they turned out to be the same thing.

## The circular metric, first

`detail_generation` decides whether tapping for detail should spin up a
generation. Its deciding metric is `drill_miss_rate >= 0.30`. Here is the code:

```python
if not self.policy.on("detail_generation"):
    await self._record(top.card.id, "drill_miss", "deferred-by-policy")
```

While the axis is off, **every drill is recorded as a miss**. So the rate reads
1.0, the axis flips on, and the policy is "justified" by the deferrals it
itself caused.

I had this backwards the first time I wrote it down. documents/19 calls the
axis "rigged by its own default" and implies it cannot leave that default.
It can, and it does, and it does so on manufactured evidence. That is worse
than a stuck axis: a stuck axis is visibly stuck, and this one looks like the
system working.

The fix is a new event kind:

| kind | meaning |
|---|---|
| `drill_miss` | we **tried**; there was nothing worth showing |
| `drill_deferred` | we never **tried**; policy said not to bother |

They look identical from outside the building — a tap, and no detail — and
they mean opposite things. Both are observation kinds carrying no topic weight,
so neither corrupts the relevance estimate; the change is only about which
question each answers.

**The deadlock this creates is deliberate.** With the axis off there are no
misses, so `drill_miss_rate` is unmeasurable, so the axis cannot turn itself
on from evidence. That is the correct state for a metric with no data. The
two honest ways out are a manual override (`--set-policy`) or a shadow mode
that generates detail nobody sees. Both are decisions. Neither is a bug to be
quietly resolved by letting the metric lie.

## The bridge

`interrupt.evidence_from_store()` derives what the ledger can actually support:

```
$ .venv/bin/python main.py --evidence
  metric              source        value
  ----------------------------------------------------
  pushes_today        no push path  0
  pushes_last_hour    derived       0
  drill_miss_rate     derived       0.0
  label_days          no source     —  <-- ABSENT, not zero
  clean_pushes        no source     —  <-- ABSENT, not zero
  n_labels            no source     —  <-- ABSENT, not zero
  queue_worth_rate    no source     —  <-- ABSENT, not zero
  rho                 no source     —  <-- ABSENT, not zero
```

### Absent is not zero

This is the part that took two attempts, because the first version of the
bridge violated its own rule.

`Budget` originally defaulted every metric to `0`. Passing that to
`policy.resolve()` produces the same axis values as omitting them — so the
policy cannot tell "measured, and it is low" from "never measured". A reader
can, and would read the first. The numbers in the table above looked like
measurements of zero and were not.

`Budget` now distinguishes them:

- `pushes_today` / `pushes_last_hour` default to **0**, because 0 is true.
  Nothing has been pushed. There is no push path yet, and "none spent" is an
  honest reading of that.
- `label_days`, `clean_pushes`, `n_labels`, `queue_worth_rate`, `rho`,
  `drill_miss_rate` default to **None**, meaning no source exists.
  `evidence()` omits them entirely, so `resolve()` sees absent metrics and
  marks those axes unresolved — the behaviour `policy.py` already documented as
  "never guessed".

A metric with a zero denominator is not 0.0 either: with no drill judged yet,
`drill_miss_rate` is unmeasurable, and reporting 0.0 would assert "drills
always succeed" about a population that does not exist.

## What the bridge proves

One metric has a real ledger source. One axis can therefore move off its
default from shipped data:

```
detail_generation -> on (drill_miss_rate=0.50: taps are routinely finding nothing)
```

The other four need **labels** — a human or a trusted judge saying "this card
was or was not worth interrupting me for". `sim/ledger.py` has `label()`, and
it is called from exactly one place: `experiments/soak.py`, against a
`VirtualWearer` whose own module docstring says it is "not part of the G1 relay
agent". There is no CLI that produces a label and no shipped code that reads
one.

So the honest summary of the policy layer today is:

| axis | metric | source | can it move? |
|---|---|---|---|
| `detail_generation` | `drill_miss_rate` | real ledger | **yes** |
| `push_enabled` | `label_days` | none | no |
| `interrupt_gate` | `clean_pushes` | none | no |
| `g1_model` | `n_labels`, `queue_worth_rate` | none | no |
| `examination_source` | `rho` | none | no |

`--evidence` exists so this question — *why is the policy not changing?* — has
an answer that is not "someone's opinion".

## Three bugs in this increment, all caught by tests

Recorded because the pattern is the point: each was invisible to reading and
obvious to a control arm.

1. **The evidence dict was flat.** `policy.resolve()` reads
   `evidence.get(axis.id)` and hands that sub-dict to the resolver. A flat
   `{"label_days": 20}` resolves every axis to its default with no error and
   no warning. Found in the previous increment; the shape is now documented at
   the definition site as well as here.

2. **`Budget.evidence()` emitted the zeros** described above. Found by reading
   my own `--evidence` output, which printed `label_days 0` next to a table
   whose whole point was distinguishing it from absent.

3. **A local import shadowed a module-level one.** `--weights` does
   `from memory import FeedbackStore` inside `run()`, which makes the name
   function-local; the new `--evidence` block, a few lines later, could not see
   the module import and raised `UnboundLocalError`. Fixed with a distinct
   local alias, because the alternative — hoisting one import — touches both
   blocks to serve one of them.

## What is still missing, and why it is not a bug

**No labelling mechanism.** This is the real blocker on the other four axes,
and it is a product decision, not a code gap: labelling means a human (or a
trusted judge) telling the system whether an interruption was correct. The
wearer's tap is the only signal, it is sparse, and it arrives late. That
limitation is why no RSI-style self-correction loop is being attempted here,
and it is the same reason the 299-clean-push requirement exists at all.

**No shipped push path.** `interrupt.py` is dry-run. Until something sends,
`pushes_today` is genuinely 0 and the budget constraint is vacuously satisfied.
The hourly recount that would make it non-vacuous is recorded as a comment in
`interrupt.py` rather than shipped as an uncalled function — the wiring audit
caught me writing exactly that function minutes earlier.

**`queue_worth_rate` is measured in simulation against a simulated opponent**
and came out at 0.004–0.006 against a ≥0.50 bar. Those numbers are real
measurements of a simulation and are not evidence about a wearer.