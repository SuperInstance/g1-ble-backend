# Panel: interaction model and candidate event stream

Reviewer angle: "the person who has actually shipped a consumer product and
cares whether this is still wanted in three months."

## 0. A correction that changes the design

**The display is write-only.** The glasses never report what is currently on
the lens, whether it was read, or whether the wearer looked away. Every piece
of state below is host-owned and must be re-derived after a restart or a
gesture.

**Paging does not work — do not use it.** `build_text_packets` sets
`page=index, total_pages=total`, but the chunk boundary is `mtu-12` **bytes**
(239 at MTU 251; 11 at MTU 23), not screen lines. The header field was being
used as a packet index. A multi-screen glance must instead be **successive
full-screen writes**, each `page=0, total_pages=1`. *(Fixed in `3ce6db0`.)*

## 1. Push vs pull — confirmed, with a reframe

Pull-dominant is right, but **pull-dominant does not mean compute-on-demand; it
means wearer-initiated interruption.** The system must poll continuously while
worn, because a queue that is empty when he looks up teaches him the glance is
pointless.

**Push budget: ≤2/hour, hard cap 4/day.**

The reasoning: an interruption costs 4–8 s of recovered attention. At 8/hour
that is ~1.7% of waking time. Above ~10/hour it becomes a watch that nags and
gets taken off. Below ~1/hour it is a watch that never speaks.

**Error budget: precision ≥99%, recall ≥90%** — deliberately asymmetric. A
missed item costs nothing extra, because the queue is the safety net and the
glance is free. A false push costs attention and, worse, teaches distrust.

**Corollary that should ship:** one mis-fire auto-suspends that source class
for 24 hours — not three. Trust collapses on the second bad interruption.

**Only three things justify a push:**

1. **Time-critical named dependency.** A meeting he is in or about to miss,
   pushed at T-10min (not T-0). A message from an explicitly starred human
   with a stated deadline.
2. **His own agent finishing a job he is blocked on** — dispatched this
   session, elapsed >2 min. Otherwise it just enters the queue.
3. **Device state** — glasses battery <10%, case open. Rate-limited to
   transitions.

Everything else (CI, email, calendar invites, agent chatter) queues silently.

## 2. Candidate event stream, ranked by value per integration effort

**1. His own agents, via a watched spool directory. Effort 1, value 9.**

The cheapest source is one where he controls the writer. His agents write
`~/.g1/outbox/*.json` on exit; the relay watches with `inotify` (event-driven,
zero poll cost):

```json
{"job":"tests","project":"glasses","ok":true,
 "line":"24 passed, 0 failed","elapsed_s":142,
 "source":"claude|gh|local","url":"..."}
```

Renders as `[*] tests green (12m)`. No integration, no OAuth, no rate limits —
and it exercises the whole path immediately.

**This is the first thing to build.** It is the only candidate source available
today without inventing infrastructure, and the other reviewers independently
reached the conclusion that nothing can be calibrated until a candidate stream
exists.

## 3. The interaction, second by second

| State | Lens shows | Transitions |
|---|---|---|
| `IDLE` | clear | head-up or tap → `PEEK` |
| `PEEK` | line 1 `3 new · 14:05`; lines 2–5 the top 4 items, one line each | tap → `ITEM`; double-tap → mark all seen, `IDLE`; head-down or 8 s → `IDLE` |
| `ITEM` | the item, ~120 chars, generated on demand | tap → `DETAIL`; double-tap → done; hold → positive mark |
| `DETAIL` | existing `Session.more()` | tap → deeper; double-tap → back |
| `ALERT` | **one line**: `Dentist 14:30 · 6 min` | deadline reached, budget allows; clears after 8 s |

**Queue lines are built without an LLM** — sender plus truncated subject. The
model is only called on `ITEM`/`DETAIL`. Dropped and unopened items never cost
a generation, which is what makes aggressive filtering affordable.

**Long queues:** at most 12 active items, 4 visible. Items from one source
collapse (`Slack ×7`). Each source has a TTL: notification 4 h, email 24 h,
calendar until start time. When full, the lowest-scored item is evicted and
logged as `overflow` so review sees it. A queue that keeps filling means the
filter is too loose — show a count, not more items.

## 4. Gesture vocabulary

Only gestures that can be distinguished are bound. `0xF5 0x04` and `0x05` are a
**single gesture that alternates bytes per press** and must never carry two
meanings.

| Byte | Gesture | Action |
|---|---|---|
| `0x01` | single tap | drill down / open queue |
| `0x00` | double tap | back / dismiss / mark seen |
| `0x17` | held | positive mark (deliberate, so unambiguous) |
| `0x02`/`0x03` | head up/down | state machine input, not actions |

Single tap in `IDLE` should open the queue. Today it is a no-op
(`session.py` returns when nothing is showing), which would be a dead gesture
if head-up turns out to belong to the firmware.

## 5. Voice

The glasses have microphones (`0xF1` LC3 audio, `0x0E` mic control). **Defer
it.** It is a second input path requiring its own protocol work, its own error
handling, and its own attention model — and it competes for the same scarce
attention budget the display is trying to protect. A wearer who cannot type
while walking needs *some* input, but a worn TouchBar plus a queue the wearer
can glance at is the smaller problem to solve first.

## 6. What kills this product

The three most likely reasons for abandonment after two weeks, unflattering:

1. **Wrong items, reliably.** Precision matters far more than recall here. If
   the third interrupt in a day is noise, the fourth is ignored and the fifth
   is not read — and the whole system is dead within a week.
2. **Latency on tap.** If drilling in feels broken or slow, the drill-down is
   the first thing abandoned, and with it the reason to own the glasses.
3. **The glasses are taken off.** Not because of software. Battery, comfort,
   prescription, social optics. Any software win is moot if they are in a case.

Note that none of these are about compression quality. The pipeline already
does that adequately.