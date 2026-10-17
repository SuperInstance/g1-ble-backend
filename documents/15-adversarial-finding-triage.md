# Adversarial finding triage

An independent reviewer was told to attack the learning store, the soak harness
and the policy axes rather than to confirm them. This records what survived,
what did not, and what is still open.

The interesting part is not the fixes. It is the three findings that turned out
to be **correct as written**, and the two that were real bugs hiding behind
deliberate design. A review that only ever confirms is not a review.

Status key: **FIXED** (verified by a test that fails without the change),
**NOT A DEFECT** (the finding is wrong, with reasoning), **OPEN** (real, not yet
addressed).

---

## FIXED — real defects, now covered by a test

### 1. The write lock was created and never acquired — CRITICAL

`memory.py` had `self._lock = threading.RLock()` and one occurrence in the
entire codebase: the assignment. Combined with `check_same_thread=False`, that
turned sqlite3's own loud safety check into silent corruption.

Measured at 8 threads x 50 writes, before the fix:

```
rows persisted      285/400     (~29% silently lost)
duplicate card ids     46       two cards sharing one id
InterfaceError         101
```

Duplicate ids are the dangerous half: `session.py` writes events and drill-down
parents using `card.id`, so events landed on the wrong card. A second, quieter
defect sat under it — with one shared connection, a transaction rolled back by
its owner was made durable by whichever unrelated thread called `commit()` next.

**Fix:** one connection *per thread* (`threading.local`), so each thread owns
its cursor, its `lastrowid` and its transaction; plus a lock genuinely held
across every write. `add_card` now reads `lastrowid` while still holding both.

**Now covered by** `experiments/store_stress.py` — 400/400 durable, 400 distinct
ids, 0 orphans, 0 dangling events.

### 2. Nothing actually left the event loop — CRITICAL

The requirement was that a DB write never stall the BLE heartbeat. The store
was *configured* for off-loop use, but no call site changed: `confirm`/`star`/
`dismiss`/`more` contain no `await` before the commit, so they committed on the
loop the heartbeat runs on.

**Fix:** every reaction now goes through `Session._record`, which dispatches via
`asyncio.to_thread`. The await is kept deliberately — fire-and-forget would let
stats drift from what is persisted.

**Now covered by** `experiments/adaptive_loop.py`, which measures a heartbeat
task against a 10ms nominal cadence: **control (inline writes) 21.8ms worst gap,
off-loop 11.1ms**. The control arm is the load-bearing one; without it, "no
stalls" could just mean an insensitive instrument.

### 3. `glance_worth()` returns 0/1/2; the ledger column is CHECK(label IN (0,1))

The first item scoring 2 raised `IntegrityError: CHECK constraint failed` and
crashed the soak on **day 1, item 19**. The experiment could not complete.

**Fix:** the ledger is fed `1 if worth >= 1 else 0` — worth-at-all is the bit
the Beta counts need. Urgency is a separate counter. This also fixed a second
defect in the same block: `earned` counted only `worth == 1`, so urgent items
fell into neither earned nor rejected while still inflating the denominator.

### 4. Features were drawn from text the wearer never saw

`session.py` built keywords from `extract_keywords(f"{query} {result.stage1}")`
— the ~300-word stage-1 answer — while the wearer reacted to 120 characters.
`FeedbackStore.add_card`'s own default already used `f"{query} {summary}"`, so
the call sites were overriding the correct behaviour with the wrong one.

**Fix:** both call sites now use `result.display_text`.

### 5. Topic weight saturated, then degenerated to alphabetical

Four taps on one card reached `MAX_WEIGHT = 3.0` and stopped responding. Six
unrelated terms tied at 3.0, so the sort fell through to its alphabetical
tie-break and produced a confident-looking ranking built from one card.

**Fix:** contributions fold per *distinct card* (one card contributes its
strongest reaction, damped logarithmically for repetition), then map through an
exponential that approaches but never reaches the ceiling. Measured: 4 taps on
one card now gives 2.23 — and is excluded from the prompt, because a topic needs
`MIN_CARDS_FOR_INTEREST = 2` distinct cards.

**Now covered by** `experiments/learning_substrate.py`.

### 6. `expand` was counted twice under contradictory readings

`EVENT_WEIGHTS["expand"] = 0.5` credited a tap as *topic interest* while
`needs_detail()` credited the same event as *detail appetite* — the sum of two
opposite interpretations (already flagged in `documents/08` §5, still live).

**Fix:** `expand` reduced to 0.25, with the reasoning recorded at the constant.

### 7. `preferences()` was unlocked by observations

The gate was `counts["events"] >= 3`, but `events` counts *all* rows, including
`OBSERVATION_KINDS`, which carry no topic weight by design. Three `repaired` or
`shown` rows were enough to unlock a personalisation block while saying nothing
about what the wearer cares about.

**Fix:** the gate counts reactions only.

### 8. Two documents counted different populations

`DESIGN.md`'s day-14 verdict query had no `decision` filter, so `shown` counted
`suppress` rows — items the wearer never saw — and the milestone could be failed
by things correctly never shown. `documents/13` read `decision='relay'`, which is
**not** a member of `CHECK (decision IN ('suppress','queue','push'))` and matched
no rows at all, so it reported a clean day-14 every time.

**Fix:** both now read `WHERE decision='queue'`, with the correction noted
in place.

### 9. `drill_miss_rate()` compared mismatched populations

Denominator was drills (`depth > 0`); numerator counted every
`outcome='unobservable'` row at any depth. Root-level misses inflated a
drill-only rate and could push it above 1.0.

---

## NOT A DEFECT — the reviewer was wrong, and here is why

### `g1_model` counts an already-known item as worth queueing

The finding: `queue_worth_rate` is `AVG(label)`, so `label=1 AND already_knew=1`
inflates the rate.

**It should.** The axis asks *"does a model suppress noise **before queueing**?"*
The question is whether an item deserved a place on the lens at all, and an item
that is relevant but already known is still relevant — it simply should not have
*interrupted*. Redundancy is an interrupt-cost question, not a queue-worthiness
question, and it is already priced separately by `already_knew` in the
milestone's `earned` metric. Collapsing the two would break the axis's stated
purpose. Left alone deliberately.

### `interrupt_gate` decides from a count rather than a rate

This is the documented point of the axis: `0.99**n <= 0.05` needs n >= 299 clean
pushes, roughly 75 days at the 4/day cap. A count *is* the rate target here.
A "corrected" version that normalised by total pushes would be less meaningful,
not more. Left alone deliberately.

---

## OPEN — real, not yet addressed

### `push_enabled` flips on a proxy instead of on the milestone verdict

The axis decides on `label_days >= 14`, which completes on the last day of the
14-day run — so pushes turn on at the exact moment milestone 1 ends, with no
evidence *about pushes* and no check that the day-14 verdict passed.
`DESIGN.md` says *"continue if earned ≥ 15, false_alarm ≤ 1, redundant < earned,
unreviewed = 0"*; `policy.py` never reads that verdict.

**This is the real defect.** Turning pushes on is the most consequential
irreversible step in the design and it currently keys off a calendar count.
The honest fix is to make the milestone verdict a computed input to the axis,
not to retune the threshold.

### `needs_detail()` still has no decay

`topic_weights()` decays by half-life; `needs_detail()` sums raw event counts
forever. A topic drilled into six months ago still shapes the compressor's
style today. The two readers now agree on the *card-count* gate but not on
decay, so they can disagree about the same topic.

### `_packet_log` grows without bound

`g1_driver.py` appends every notification and nothing trims it, while
`wait_for` slices `self._packet_log[since:]` on each poll. Pre-existing, slow
leak over a long session.

### A corrupt database crashes startup with a traceback

`FeedbackStore(path)` on a non-database file raises `DatabaseError`, and
`main.py`'s handlers cover only `BleakError` / `KeyboardInterrupt` /
`CancelledError`. The user sees a stack trace rather than a usable message.

---

## Still unproven (not findings — absences)

- **Does the prompt injection improve anything?** The substrate is now measured
  and the injection is gated, but the A/B promised in `.context/llm.md` is still
  unrun. `personalise=False` makes it runnable without a code change.
- **Is the substring salience gate sound?** `mentions()` does a case-insensitive
  substring test. It is intentionally conservative (withhold unless clearly
  relevant) but has not been characterised on real text.
- **No G1 hardware.** Every scan, connect, notify and render path remains
  unproven on device. Nothing in this document changes that.