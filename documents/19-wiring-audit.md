# 19 — The wiring audit: what is described as working, and is not

*Date: 2026-10-16. Follows [18](18-personalisation-measured.md).*

---

## How this started

Removing the interest injection at `5b873e8` turned up something the
falsification could not have found.

`plugins.RefinementPlugin` has a docstring saying its cached profile "is what
gets injected into the stage-2 prompt from then on." Its only consumer,
`plugins.preferred_profile()`, has never had a caller in any commit —
verified with `git log -S`, which returns exactly one commit, the one that
introduced both symbols.

So one of the three answers to "what personalises this?" was described as
live, was believed to be live, and was wired to nothing.

That is a defect class, not an incident. A bug that crashes announces itself.
This one costs nothing to run and everything to trust, and it is invisible to
exactly the tools already in use: a reviewer reading the docstring sees a
working feature, and a test asserting on the prompt sees a block that is not
there. **It is a mechanical property of a symbol table, so it should be
checked mechanically.** Hence `experiments/wiring_audit.py`, which reports
shipped functions with no shipped caller.

## What the audit found

Two independent passes — one mechanical, one reading — over 5,900 lines.

| # | finding | severity |
|---|---|---|
| 1 | `plugins.py` entirely dormant: `discover`, `install_builtin_plugins`, `resolve_config`, `maybe_refine`, `preferred_profile` all uncalled. No shipped module imports the file. | HIGH |
| 2 | The glanceability gate **never ran on the default path** | HIGH |
| 3 | `README.md` asserted the removed injection still read the store before the models | HIGH |
| 4 | Policy: saved overrides were ignored at runtime; 4 of 5 axes had no shipped consumer | HIGH |
| 5 | The interruption decision **does not exist** in shipped code at all | HIGH |
| 6 | `enforce_word_budget` kept with a comment claiming live call sites it did not have | MED |
| 7 | `WearerContext` docstring claimed it was taken "BEFORE the LLMs run" | MED |
| 8 | `.env.example` advertised an interrupt gate for `jev.py` that does not exist | MED |
| 9 | `G1Arm.next_seq()` uncalled; the text path uses a per-send counter, so `self._seq` never advances | MED |
| 10 | `top_topics()`/`topic_weights()` dead, justified by a plugin seam that cannot load | MED |

## The two that mattered

### The glanceability gate had stopped running

The gate lived inside `condense_for_glasses`. When the architecture flipped to
one call by default (`d646cfd`), stage 2 stopped running — and so the gate
stopped running. The one-call path applied the character budget and nothing
else.

Nothing broke. No test failed. The README kept advertising "stage 1 → stage 2
compress + glanceability gate + repair" as the pipeline shape. A measured
mechanism — the 0.838-vs-0.688 result from the gate fix — was running only on
the path the design had moved away from.

Fixed by extracting `finalize_display_text()`, which every display path now
calls, so a future architecture change cannot drop one of the three steps
again.

An honest note on the size of the fix: the gate's observable effect is a log
line and, where a fact is genuinely buried, a reorder. Suppression is
deliberately forbidden — it is the most expensive thing this system can do —
and `repair_buried_fact` cannot manufacture a fact for genuinely vague text.
So the fix restores the check, not a visibly different display. It was still
worth doing: the mechanism now runs where it is documented to run.

### The interruption decision was absent entirely

Before this work the repository had a policy layer deciding whether pushes are
allowed, a ledger recording reactions, a queue described at length in
`DESIGN.md` and documents/07 and documents/08 — and **no shipped function that
ever made the decision**. The only push decision in the repo was
`experiments/soak.py`, scoring simulated candidates against a virtual wearer,
and its own report concludes `axes ever move off defaults: 0/5`.

That is not a bug. It is an unbuilt feature, and the audit's contribution is
naming it: the docs describe a push system in the present tense throughout.

## What was built: `interrupt.py`

The decision now exists as a **pure function**. It takes candidates, a budget
and a policy, and returns what should happen to each. It imports no BLE, writes
to no radio, and reads no ledger.

That is the whole design constraint, and it comes from the hardware situation:
every claim this project makes has to be checkable with no glasses, because
there are no glasses. A function with no I/O can be measured; a function that
writes to a BLE characteristic cannot be measured at all, which is the state
the interrupt logic was in.

```
$ .venv/bin/python main.py --queue --queue-dir ./spool
  action gate         G2   src      why
  queue  eligibility  no   cal      declined invitation; not the wearer's meeting
  PUSH   eligible     yes  cal      accepted, starts in 4m (T-10)
  PUSH   eligible     yes  glasses  battery 4% (below 10%)
  queue  budget/axis  no   build    2/2 hourly pushes used; budget exhausted
```

Gate order is budget → axis → eligibility → order. Budget is first because
being out of budget is not a matter of evidence. Eligibility is G2 from
`DESIGN.md`. **Nothing is ever dropped** — a candidate that fails a gate is
queued, because silence is the default state and silent discarding is the one
outcome this system may not choose.

The budget is not re-implemented here. `plan()` re-resolves the axis after each
push, so five eligible candidates in one batch still produce at most
`MAX_PUSHES_PER_HOUR` between them, using the constants in `policy.py`. A copy
of that arithmetic would be the copy nobody updates.

### Three bugs the tests caught while building it

Each was found by a test that had a control arm, which is the only reason it
was found at all.

1. **`Budget.evidence()` had the wrong shape.** `policy.resolve()` reads
   `evidence.get(axis.id)` — nested per axis. A flat `{"label_days": 20}`
   resolves every axis to its default with no error and no warning. The
   control arm ("with label history the same candidates DO push") is what
   turned a silently-ignored argument into a visible failure.

2. **`urgency()` compared unlike units.** It ranked minutes-until-meeting
   against seconds-blocked, so a job blocked ten minutes sorted ahead of a
   meeting starting in two. Not a tuning error — two quantities with no
   common scale, which looks fine until the numbers get large. Now a tuple:
   tier first, within-tier key second.

3. **`queue.py` shadowed the stdlib `queue` module**, which asyncio imports
   internally. Caught by `adaptive_loop.py` failing with
   `module 'queue' has no attribute 'SimpleQueue'`. Renamed to `interrupt.py`.

The third is the embarrassing kind, and it is worth recording because the
honest summary of this work is that the guard caught me immediately after I
built it: the audit flagged `Budget.prune()` — a method I had just written,
with a docstring explaining how it prevented a bug, called by nothing. It is
now a comment recording the gap rather than a function pretending to close it.

## The standing guard

`wiring_audit.py` does not just report; it **fails**. Every uncalled shipped
function must appear in `ACKNOWLEDGED_DEAD` with a reason and a revival
condition. An unlisted one is a failure.

That is the part worth arguing for. A report is read once and then ages. A
failure shows up in the diff when someone adds the next uncalled function, and
forces them to write down whether it is an extension point, a compat shim, or
a bug. Seventeen entries now carry that reasoning, including the three plugin
ones, which say plainly that wiring them is a **decision rather than a line of
code** — `maybe_refine()` would re-enable the mechanism falsified in
documents/18, and `resolve_config()` would let a plugin reroute models with no
measurement behind it.

## What was fixed versus what was only recorded

Fixed, because they were wrong:

- the glanceability gate now runs on the default path
- `--set-policy` overrides now reach a running `Session` (`resolve()` →
  `load()`; the operator's confirmation screen had been disagreeing with the
  behaviour they then got)
- stale docstrings in `README.md`, `memory.py`, `pipeline.py`, `plugins.py`,
  `.env.example`

Recorded but deliberately not changed:

- **the plugin seam stays dormant.** Wiring it is one line per entry point and
  would be wrong twice over, per above.
- **`next_seq()` stays uncalled.** Whether the G1 firmware needs a monotonic
  `0x4E` sequence is a hardware question this repository cannot answer. It was
  already tracked in `.context/ble.md`; the audit independently rediscovered it,
  which is mild evidence the tracking is in the right place.

## What this says about the project

Three of the six HIGH findings were **stale residue of a change I had made in
this session or the one before** — the README paragraph about the store read,
the `WearerContext` "BEFORE the LLMs run" claim, the gate that vanished with
the architecture flip.

The pattern is specific: removing a feature updates the code and leaves every
sentence that described it. Those sentences are the ones a future reader
trusts, because they are prose and prose does not get flagged by a type
checker.

The standing guard covers the mechanical half of this. The other half is a
rule this document should end on:

> When a change removes a mechanism, grep for the sentences that mention it.
> Not the symbols — the sentences.

## Known open items

- **The ledger is not wired to the interrupt decision.** `push_enabled` needs
  14 days of tier labels; labels are written only by `sim/`, and no CLI
  produces one. `interrupt.py` takes a `Budget` from its caller, and no caller
  builds one from `FeedbackStore`. That is the next real increment, and without
  it no axis can ever leave its default.
- **`detail_generation` is rigged by its own default.** With the axis off,
  every drill tap records `drill_miss`, so the metric that would flip it reads
  100% by construction. One axis of five is load-bearing, and it is the one
  that cannot be measured while it is off.
- **`next_seq()`** — hardware question, above.
- **`jev_gate.py`** — blocked on a real `apikey_…` key.