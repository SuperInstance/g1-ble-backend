# 21 — Phase 3: two bugs that would have shipped, and an unreachable target

*Date: 2026-10-16. Follows [20](20-evidence-bridge.md).*

---

## Where this phase came from

documents/20 ended with the honest state: one axis of five has a data source,
and the other four need labels that nothing produces. That is a product
question, so this phase put two design agents on it in parallel — one
researching how interruptions get verified at all, one designing the labelling
mechanism against the real code surface — and a third on the safety envelope
for enabling a live push.

This document covers what came back. The literature track is still running and
will be its own document; nothing here depends on it.

## Three bugs, all of which would have shipped

None of these were found by reading. Two came out of the design review and one
out of trying to connect two modules that had been written apart.

### 1. A manual override looked like a measurement

`--set-policy push_enabled=on` produced a `Decision` with
`enough_evidence=True`. That is the field every gate in the system reads to
ask "is this settled?" So pinning an axis by hand made it vanish from
`Policy.unresolved()` and any check would accept a human assertion as
evidence.

Verified directly before changing anything:

```
option          : on
enough_evidence : True          <- a person typed this
listed unresolved: ['examination_source', 'g1_model', 'interrupt_gate', 'detail_generation']
```

The pin also persists in `policy.json`, so the failure mode is a specific one:
someone flips an axis on to watch something happen, forgets, and weeks later a
process has been running on the pin. The canonical accident is a test switch
that outlives the test session.

Fixed three ways, because one was not enough:

- overrides are now `manual=True, enough_evidence=False` — they still take
  effect, but they cannot masquerade;
- `Policy.manual()` names them and `Policy.settled_by_evidence()` refuses
  them, so anything irreversible can say "I need a metric, not a person";
- overrides can carry a **TTL**, checked when the policy *loads* rather than
  when it is written, so a pin lapses on its own instead of needing somebody
  to remember it exists. `--override-ttl-hours`. Malformed TTL files keep the
  override rather than silently discarding an operator's intent — a
  surprising drop is worse than a pin lasting slightly too long.

There is a control arm in `experiments/policy_axes.py`, because "never trust
the policy" is not the claim: an axis settled by `label_days >= 20` still
returns `True` from `settled_by_evidence()`.

### 2. The budget counter could only climb

`Budget.spend()` incremented `pushes_last_hour` and nothing ever decremented
it. That is correct for the span of one `plan()` call and wrong for a process:
a long-running loop would reach 2/2 after two pushes and then refuse
**forever**, with a log full of successful pushes and nothing indicating a
problem.

A silent daemon is worse than a broken one. A broken one gets looked at.

The reason it was wrong is that nothing recorded *when* a push happened — the
counter was the only record. Fixed by giving it a real one:

```
push_events(id, ts, cls, source, text_hash, outcome)
```

with `record_push()` / `settle_push()` / `push_counts()`. Intent is recorded
**before** the BLE write, not after, because a host that dies between deciding
and sending must still spend the budget. And the budget is spent on
**attempt**, not on success: a card that half-rendered on one lens did cost the
wearer attention, and a flapping link should fail quiet at four wasted pushes
a day rather than hammer through them.

`experiments/interrupt_gate.py` §11 asserts the property that matters — an
hour later the hourly budget is **0 again**. An increment-only counter cannot
produce that number.

This also closes the `Budget.prune()` note from the previous commit, which
said the recount "gets closed when a push path exists". It does not wait for
the path: the fix is the storage, and the storage is what everything else
hangs off.

### 3. The simulator and the decision module disagreed on a field name

`sim/events.py` has always written the documented spool format with the key
`"class"`. `interrupt.Candidate.from_json()` read `"cls"`, matching its own
dataclass field.

So every candidate the simulator produced parsed as class `unknown`, failed
G2 with `class 'unknown' is not in the G2 rule set`, and queued forever.
Nothing raised. The queue simply decided nothing was ever worth interrupting,
which reads exactly like a conservative system working, and the cause — a key
name — appears nowhere in the output.

Found by round-tripping the simulator through its own documented format. The
rule worth keeping:

> If two modules agree on a contract, test the contract.

Now `experiments/interrupt_gate.py` §10 round-trips real `sim` output through
the real decision and asserts the two eligibility rules agree.

## The observability cell

`g1_driver` decodes six wear-state events — `0x06` worn, `0x07` removed, `0x08`
in case lid open, `0x0B` in case lid closed, `0x1E`/`0x1F` dashboard open and
closed. `handle_touch` looked each one up in the action table, found nothing,
and returned. The firmware was emitting free observability evidence and the
host was throwing it away.

That gap is load-bearing for the whole labelling question. "The wearer did not
react" has two causes: they looked away, or the glasses were in a pocket. Only
the first is evidence about the wearer. Treating the second as a negative is
how a review queue learns that cards in a pocket are worthless — which is
exactly what `documents/09-schema.sql:59-61` warns about when it says *never
examined is missing data, NOT a negative label*.

So `Session.observable` tracks those events, and every card records
`shown observable=0|1` at the moment it reaches the lens. Cost to the wearer:
zero. It is telemetry the hardware emits whether or not anyone listens.

This is the cheapest real progress available on the labelling track, and it is
a prerequisite rather than a contribution: until "observable" is a queryable
cell, stratum B of any review design is undefined.

## The arithmetic, and it changes the target

The `interrupt_gate` axis wants ≥299 clean pushes to claim 0.99 precision at
95% confidence. The bound itself is right:

```
0.99^n ≤ 0.05  →  n ≥ ln(0.05)/ln(0.99) = 298.07  →  n ≥ 299
```

What is wrong is the reading of it as a roadmap item.

**First, pushes cannot start on day one.** `push_enabled` needs 14 days of tier
labels before it will resolve on. Total elapsed to a first push: 14 + 75 =
**89 days**, not 75.

**Second — and this is the part that matters — a cumulative count of clean
pushes is not evidence.** A gate that fires 299 times and is wrong 5 times has
*demonstrated* 98.4% precision, not 99%. The correct construct is a **streak
that resets to zero on the first false push**. Expected trials to reach 299
consecutive clean pushes, at `q = 1 − p`:

| true false rate `p` | P(299 clean, first try) | E[trials] | days @4/day |
|---|---|---|---|
| 0.0005 | 0.861 | 323 | 81 |
| 0.001 | 0.741 | 349 | 87 |
| 0.002 | 0.550 | 410 | 102 |
| 0.005 | 0.223 | 695 | 174 |
| **0.010** | **0.050** | **1,919** | **480** |
| 0.020 | 0.002 | 20,957 | 5,239 |

The cliff is at `p ≈ 0.002`. Below a 0.2% false rate the axis is reachable in
about three months. At exactly 1% — the target precision — the probability of
an uninterrupted clean run of 299 is **5%**, and you should expect roughly
1,900 pushes, about **480 days** at the cap.

**`interrupt_gate = learned` is unreachable in practice, and that is the
correct outcome rather than a bug.** `documents/13:30` already says it: a
learned gate can never be shown to be good enough, so the interrupt gate is a
whitelist of things that are true by definition. The 299 is a guard against
silently relaxing the rules, not a milestone. It should be left on `rules`.

### Queued items must not count toward `clean_pushes`

Worth stating because it is easy to do accidentally and it is the failure
`.context/policy.md:50` predicts. Three reasons, in increasing order of
severity:

1. **Denominator.** `clean_pushes` is a claim about `P(no false push | the gate
   pushed)`. A queued item was never pushed, so it cannot be a false push.
2. **It is logically perverse.** A queued item labelled "not worth" is a true
   negative *by construction* — the system declined and the wearer agrees.
   You could reach 299 on pure noise: 299 queued items all labelled worthless
   proves nothing, because nothing was ever interrupted.
3. **It destroys the bound.** The rule-of-success argument needs the 299 to be
   a sample of the actual decision distribution. Including rows never subject
   to the decision is selection bias in its purest form.

The legitimate use is different and should be kept: queued items feed
`queue_worth_rate`, which is literally the question *"was the queue worth
glancing at"*. So the two axes need **different mechanisms**:

| axis | metric | can it move? |
|---|---|---|
| `g1_model` | `queue_worth_rate` over queued labels | **yes — in weeks** |
| `interrupt_gate` | clean-push streak | effectively never |

That split is the most useful thing this phase produced. The one axis that can
move is not the one the project has been counting toward.

## The rollout ladder

From the safety-envelope review, trimmed to what is buildable and worth
building with no hardware:

| rung | what | build? |
|---|---|---|
| 0 | decision + dry run | done |
| 1 | budget counted from durable timestamps | **done this commit** |
| 2 | kill-switch + auto-suspend state machine, shipped dormant | **done this commit** |
| 3 | a `LinkStatus` seam — gate logic tested against a fake link | **done this commit** |
| 4 | **run the existing CLI on real hardware** | not code — a session |
| 5 | live push, operator-armed, one class | not until 4 |
| 6 | unattended | not until 5 |

## Rung 2, built: stopping, and quieting a bad source

Two rules the panel wrote down and nobody built:

> **documents/07:** "one mis-fire auto-suspends that source class for 24
> hours — not three. Trust collapses on the second bad interruption."
> **documents/13:** "one `n` in review suspends that push type for 24 h."

### The kill-switch is a file, not a signal

`~/.g1/pushing.armed`, re-read on every cycle rather than cached at startup.
The reason is the failure mode it exists for: a process that has stopped
answering the terminal that started it. A stop control you can only reach
through that terminal is not a stop control.

Absence of the file means **disarmed**. If the check fails to run — missing
file, unreadable, exception — the safe direction is silence, and that is the
direction coded.

A TouchBar gesture would be better UX and is deliberately **not** built: it
depends on `0xF5` decode correctness, which is unverified on hardware, and
`0x18`'s meaning is unknown. Capture first; bind later.

### The anti-starvation constraint is the whole problem

The panel also wrote that signal starvation is what kills this product. So a
suspend mechanism is dangerous by construction, and the naive version is a
way to mute yourself permanently. Four properties, each asserted in
`experiments/suspend_gate.py`:

| # | property | asserted by |
|---|---|---|
| 1 | **Absence never suspends.** 200 never-examined pushes suspend nothing. | §2 |
| 2 | **Transport failure never suspends.** 50 undelivered pushes suspend nothing. | §2 |
| 3 | **One explicit refusal suspends one class, for 24h.** | §2, §3 |
| 4 | **Expiry is unconditional.** A second refusal restarts the clock; nothing else extends it. | §3 |

Property 2 is the one worth arguing for. If a dead radio counted as a bad
source, a flaky link would mute meetings, battery alerts *and* blocked jobs
at once — which is precisely how an auto-suspend rule becomes a global mute by
accident, and precisely the failure the budget rule was supposed to prevent.
Transport failures route to link health, which does not exist yet.

There is no escalation path from a class suspension to a global mute. Silence
is already the default posture, so escalation would only ever remove
information.

And suspended means **queued**, not dropped — verified with a control arm,
because the first version of that check passed without reaching the
suspension gate at all (it queued on the budget gate instead, since the test
budget had two pushes on it). A class muted for a day is quiet for a day, not
gone for a day.

The 24 hours is a **named constant with an honest origin**, not a derived
number: `sim/wearer.py` models trust decaying per bad interruption and
recovering at 0.03/day, and `sim/README.md` records that the simulator
*deliberately violates* this rule. It is a choice the panel argued for.

### Operator surface

```
--kill-switch {arm,disarm,show}   the stop control
--suspend CLASS --suspend-reason R suspend one class for 24h
--reinstate CLASS                  clear early; operator action, never automatic
```

`--suspend digest` **exits 2 with an explanation**: `digest` can never
interrupt, so suspending it would be a no-op that looks like a safety feature.
That is the same instinct as removing `enforce_word_budget` — a control that
does nothing is worse than no control, because it is believed in.

What must be true that **cannot** be checked offline, named so it is not
smoothed over later:

1. **The mis-fire signal does not exist.** The display is write-only; nothing
   reports what reached the lens or whether it was read. Attribution requires
   matching the next TouchBar interaction back to a push by timestamp, and
   that inference is unmeasured. So the 24h auto-suspend rule — "a corollary
   that should ship" per documents/07 — currently has **no trigger**.
2. Whether the `0x4E` `seq` byte must be monotonic. If it does, the second
   push of every session silently fails to render — indistinguishable from an
   over-strict eligibility gate, and it would burn weeks.
3. Whether MTU > 23 is achievable on BlueZ, and whether the 2-byte heartbeat
   is correct at all.
4. Whether the host survives a day. 32s heartbeat versus a closing laptop lid.
   A push loop that dies at 40 minutes looks exactly like a quiet day.

## Rung 3, built: the link gate

`interrupt.LinkStatus` is a struct and `check()` is a pure predicate, so the
whole gate is exercisable with no G1 and no BlueZ. That is the entire reason
for this shape: the testable artefact has to be the *data*, with the logic
reading it, because logic buried in the driver can only be tested by
connecting a physical device.

### The failure it prevents

`EvenG1Controller.arms` filters to whichever arms are connected, and
`send_text_packet` takes `min(mtu)` across the survivors. So a dead right arm
returns `[left]`, the caller proceeds, and the card renders on **one lens with
no error raised anywhere**.

That is not "half an interruption". It is a different and more alarming one:
text appears on one side of the wearer's face with nothing on the other, and
they cannot tell whether the glasses are broken or the system is broken. The
gate refuses instead, and the refusal says why in those words.

The second failure is quieter. `_heartbeat_loop` used to swallow every
exception and log a warning, so a heartbeat task that had **died** looked
exactly like a healthy link by any other observable. It now stamps
`_last_heartbeat` on every confirmed send, and the gate refuses on staleness —
the only observable that separates "fine" from "looks fine because nobody has
asked it anything in a minute".

```
both arms, MTU > 23 each, last heartbeat < 20s   -> may push
right arm dead                                     -> REFUSED ("one lens")
MTU 23 anywhere                                    -> REFUSED (no notifications flow)
heartbeat 45s old                                  -> REFUSED (link drops at ~32s)
heartbeat never seen                               -> REFUSED ("cannot prove")
```

`controller.link_status()` populates it, and the REPL `:stats` prints it, so
it is read rather than being a struct that exists for its own sake.

**What this does not prove:** that a real G1 reports any of this truthfully.
`g1_driver` has never touched a device. The logic is verified against a
hand-built fake; the device is not. Those are different claims and
`experiments/link_gate.py` says so in its own header.

## What was deliberately not built

- **A "useless" swipe on `0x18`.** Its meaning is unknown and there is zero
  capture data. The repo has already retracted one guess in this exact area
  ("that was a guess dressed up as a design"). Capture first.
- **Head-up as a label.** Barred by `documents/13:32` and independently gated on
  the `ρ ≥ 1.6` calibration walk that has not happened. Using it at runtime
  would pre-empt the axis that exists to decide whether it is safe.
- **TouchBar reactions folded into a `label` column.** Explicit and implicit
  signals are separated structurally in `09-schema.sql` for a reason.
- **A live push path.** See the ladder. Building toward one before any device
  has validated the wire format would produce the most dangerous artefact this
  project could ship: a daemon that pushes to a real face.

## Still blocked, unchanged

- **No hardware.** Every rung past 3.
- **`jev_gate.py`** — needs a real `apikey_…` key. The available file is a
  secret-manager reference; all 40 calls have returned 401.
- **zcode** — `project_token_login_failed` again this session (8th attempt).
  It needs `zcode login` from a browser.
- **The literature track** — running; will be [22](22-interruption-verification.md)
  when it lands.