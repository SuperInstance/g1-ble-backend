# g1-ble-backend

A custom developer backend for **Even Realities G1** smart glasses, built from
scratch on [`bleak`](https://github.com/hbldh/bleak). Scan for the glasses,
connect, render text to the waveguide, capture TouchBar packets, and run a
two-stage LLM pipeline over a question to mirror the answer onto the lenses.

Plus a **policy layer** that turns every unresolved design disagreement into a
switch with a deciding metric, rather than an argument.

No vendor SDK. No reverse-engineering dependency. Just bleak, the protocol, and
SQLite.

> **Status.** Everything here is verified against a real environment *except the
> glasses themselves* — no G1 hardware was available during development. The
> dev host is WSL2 with no BlueZ, so the entire BLE path is unproven on device.
> See [What is verified](#what-is-verified-what-is-not).

---

## Contents

- [Quick start](#quick-start)
- [What this actually does](#what-this-actually-does)
- [The hardware, and why it shapes everything](#the-hardware-and-why-it-shapes-everything)
- [Using the LLM pipeline](#using-the-llm-pipeline)
- [Providers: any local or cloud model](#providers-any-local-or-cloud-model)
- [Policy axes: disagreements as switches](#policy-axes-disagreements-as-switches)
- [The interrupt decision](#the-interrupt-decision)
- [Experiments](#experiments)
- [Simulation](#simulation)
- [What is verified, what is not](#what-is-verified-what-is-not)
- [Documentation index](#documentation-index)
- [Repository layout](#repository-layout)

---

## Where this is going

A short map, because the interesting part of this repository is a chain of
things that looked finished and were not.

**The product.** A relay agent that watches a stream of events and decides
whether to interrupt a person walking around wearing glasses. The promise is
silence by default: the most expensive thing it can do is interrupt for
something worthless.

**What exists today.** A pipeline that turns a typed question into 120
characters on a lens, a learning ledger, a policy layer, and — since the last
few commits — the interrupt *decision* as a pure, fully offline-testable
function. `main.py --queue` runs it against a spool and prints what it would
do. **Nothing ships that sends a push.**

**Three classes of work, in the order they happened:**

| | what | why it matters |
|---|---|---|
| 1 | Measure, falsify, remove | The interest injection was measured and deleted rather than disabled ([18](documents/18-personalisation-measured.md)). A documented-but-unwired plugin seam and a decision function that did not exist were found by a mechanical audit, not by reading ([19](documents/19-wiring-audit.md)). |
| 2 | Give the policy an input | No code computed any policy metric, so all five axes resolved to their defaults forever. Splitting `drill_miss` from `drill_deferred` stopped a metric measuring its own deferrals ([20](documents/20-evidence-bridge.md)). |
| 3 | Build the safety envelope offline | Budget counted from real timestamps rather than a counter that only climbs; a kill-switch; a 24h auto-suspend that cannot be triggered by absence; a link gate that refuses a one-lens card. Three bugs found along the way that would all have shipped ([21](documents/21-phase3-findings.md)). |

**And the honest limit.** The literature says you cannot both collect real
labels and learn per-event from the same stream — that is an identification
problem, not an engineering one ([22](documents/22-interruption-verification.md)).
So the next decision is *which phase this system is in*, not which feature to
add next.

**Blocked on a person, not on code:** a G1 on a BlueZ host (rung 4 of the
rollout ladder is a ten-minute session, not an engineering task), a real
`apikey_…` for Jev, and `zcode login`.

---

## The interrupt decision

`interrupt.py` holds the push-vs-queue decision as a **pure function**.
Candidates in, verdicts out. No BLE, no radio, no ledger — which is the whole
constraint, because every claim here has to be checkable without glasses and
there are no glasses.

```bash
main.py --queue --queue-dir ./spool      # what would happen (dry run)
main.py --queue --queue-record           # ...and write it to the ledger
main.py --review                         # a human labels what was decided
main.py --evidence                       # which policy metrics are real
```

The gates, in order, each refusing rather than degrading:

1. **kill-switch** — a file, re-read every cycle, so a process that has stopped
   answering its terminal can still be stopped. Absence means disarmed.
2. **budget** — ≤2/hour, ≤4/day, counted from durable timestamps. A failed
   attempt still spends it.
3. **policy axis** — `push_enabled`, which with no label history is off.
4. **suspension** — one mis-fire mutes that source class for 24h. Never
   triggered by absence, never by a transport failure, never escalated to a
   global mute, and suspended means **queued**, not dropped.
5. **link** — both arms, MTU negotiated, heartbeat fresh. A dead right arm is
   refused rather than rendering a card on one lens.
6. **G2 eligibility** — the deterministic rules from `DESIGN.md`.

Nothing is ever dropped. A candidate that fails a gate is queued, because
silence is the default and silently discarding things is the one outcome this
system may not choose.

The chain that makes it real now closes end to end:

```
decide -> record -> a human labels it -> --evidence shows derived metrics
```

Three of the five policy axes moved from "no source" to "derived" the first
time that loop was run. The fourth, `rho`, needs a calibration walk on
hardware. The fifth, `interrupt_gate`, is arithmetic that says it should stay
on `rules` forever — see [21](documents/21-phase3-findings.md).

---

## Quick start

```bash
./setup.sh                        # venv, dependencies, Bluetooth preflight
source .venv/bin/activate         # Windows: .venv\Scripts\activate

python main.py --selftest         # 42 offline checks. No radio, no API key.
python main.py --policy-axes      # every open design question and its deciding metric
python main.py --scan-only        # see what advertises
python main.py                    # interactive session
```

BLE-only use works with no API key and nothing installed beyond `bleak`:

```bash
python main.py --one-shot --text "hello from python"
python main.py --scan-only --verbose
```

---

## What this actually does

```
terminal query / TouchBar tap
        │
        v
  Session.ask() / Session.more()          session.py    card stack, drill-down
        │
        ├─ pipeline.run_query()            pipeline.py   one call by default;
        │       │                                        two-stage via --two-stage
        │       │                                        + glanceability gate + repair
        │       │                                        (finalize_display_text, both paths)
        │       v
        │   EvenG1Controller.send_text_packet()         0x4E packet -> BOTH arms
        │       g1_driver.py     scan, connect, MTU, heartbeat, TouchBar capture
        │
        ├─ interrupt.plan()                   interrupt.py DRY RUN TODAY: decides
        │       │                                        push vs queue, no BLE write
        │
        ├─ memory.FeedbackStore              memory.py    cards + reactions, SQLite
        └─ policy.Policy                     policy.py    contested decisions, resolved
```

Two LLM stages, deliberately separated:

- **Stage 1** reasons and writes a normal, complete answer.
- **Stage 2** is a strict structural filter that squeezes it into the **120
  characters** the waveguide can actually show.

Splitting the roles keeps reasoning quality high and makes display formatting a
separate, cheap, easily-tuned step. Asking one model to do both degrades the
reasoning.

### The display budget is in characters, not words

The official app lays out ~5 lines of ~24 characters, so one screen holds about
**120 characters**. A 25-word cap is not equivalent — 25 words runs to ~199
characters and renders as 9 lines. The ceiling is applied in code
(`enforce_display_budget`), unconditionally, because a limit tied to physical
dimensions is not something to take on faith from a language model.

---

## The hardware, and why it shapes everything

| Fact | Consequence |
|---|---|
| The G1 is **two BLE peripherals**, one per arm, named `G1_<ch>_<L\|R>_<sn>` | Text must be written to both, left then right. A single connection reaches one arm. |
| **MTU must be negotiated** or no notifications arrive | bleak handles this on Windows/macOS; BlueZ needs the private `_acquire_mtu()`. Without it, TouchBar taps never appear. |
| **The link drops after ~32 s without a heartbeat** | A blocking `input()` starves the event loop that runs the heartbeat, which kills the link. Terminal reads go through `asyncio.to_thread`. |
| `0x4E` `page`/`max_page` index **screens**, not BLE fragments | Conflating them is invisible at MTU 251 and wrong at MTU 23 — a card renders as four fragment screens. |
| `0xF5 0x04` / `0x05` are **one gesture that alternates bytes per press** | Mapping them to two actions makes the meaning flip-flop. Left deliberately unbound. |

Full protocol reference: **[documents/01-hardware-facts.md](documents/01-hardware-facts.md)**

---

## Using the LLM pipeline

```bash
export OPENAI_API_KEY=sk-...

python main.py                                  # interactive REPL
python main.py --ask "why is onion skin layered"
python main.py --no-text --interactive          # connect without sending anything
```

In the REPL:

| command | effect |
|---|---|
| `<any text>` | full pipeline, mirrored to the lenses |
| `:more` | drill down on the current card (same as a single tap) |
| `:dismiss` | back out one level, or clear (same as a double tap) |
| `:confirm` / `:star` | mark the current card good / important |
| `:text <literal>` | send literal text, bypassing the LLM |
| `:stage1 <query>` | run only stage 1 and print the full answer |
| `:stats` / `:prefs` / `:config` / `:battery` | session, learning, config, power |
| `:quit` | disconnect |

**Failures never drop the Bluetooth session.** A stage-2 failure falls back to
deterministic local truncation of stage 1; a stage-1 failure falls back to a
static line. The BLE write happens *last*, so a slow or broken API never delays
the link.

---

## Providers: any local or cloud model

Any server implementing `POST {base_url}/chat/completions` works. There is
nothing vendor-specific in the code path.

```bash
python main.py --list-providers
```

| preset | endpoint | key |
|---|---|---|
| `openai` | `api.openai.com` | `OPENAI_API_KEY` |
| `ollama` | `127.0.0.1:11434/v1` | **none needed** |
| `lmstudio` | `127.0.0.1:1234/v1` | none needed |
| `llamacpp` | `127.0.0.1:8080/v1` | none needed |
| `vllm` | `127.0.0.1:8000/v1` | none needed |
| `openrouter` / `groq` / `together` / `deepseek` | hosted | provider-specific |
| `custom` | whatever you set | — |

Local presets ship with **no hardcoded model name**: the driver asks the server
what it serves via `GET /v1/models` and reports what it picked, so a fresh
checkout works against whatever you have already pulled.

```bash
python main.py --provider ollama --list-models
python main.py --provider ollama --llm-check    # probe without touching the radio
```

Anything not listed:

```bash
export OPENAI_BASE_URL=http://some-host:8000/v1
export OPENAI_API_KEY=anything
export OPENAI_MODEL=<model-name>
```

Configuration: **[.env.example](.env.example)** · implementation:
**[pipeline.py](pipeline.py)**

---

## Policy axes: disagreements as switches

Several reviewers reached genuinely different conclusions and none had the
hardware to settle them. Picking a side would be an argument dressed as a
design. So every open question is a **declared axis** with its deciding metric
named *before* the data exists.

> **Expand at setup, streamline at runtime.** At install the axes calibrate
> against real measurements. At runtime one resolved `Policy` is read and the
> hot path decides nothing.

```bash
python main.py --policy-axes                    # every axis, options, deciding metric
python main.py --policy                         # what is resolved, what is still open
python main.py --evidence                       # which metrics are real, which have no source
python main.py --set-policy g1_model=on         # pin one by hand; persists
```

| axis | options | default | settled by | source today |
|---|---|---|---|---|
| `examination_source` | `touchbar` / `head_up` / `both` | `touchbar` | ρ = θ/A_b ≥ 1.6 from the calibration walk | none |
| `g1_model` | `on` / `off` | `off` | `queue_worth_rate` ≥ 0.5 over ≥100 review labels | none |
| `interrupt_gate` | `rules` / `learned` | `rules` | ≥299 clean pushes | none |
| `detail_generation` | `off` / `on` | `off` | `drill_miss_rate` ≥ 0.30 | **the ledger** |
| `push_enabled` | `off` / `on` | `off` | a full fortnight of tier labels | none |

**Safety property, tested:** with zero evidence every axis holds its
conservative default. An axis cannot be switched on by opinion — and a
`--set-policy` pin cannot either: overrides are recorded as `manual=True,
enough_evidence=False`, so `settled_by_evidence()` refuses them and `--evidence`
names them. `--override-ttl-hours` makes a test pin expire on its own when the
policy loads.

**`interrupt_gate` is not a milestone, and the arithmetic says so.** Its ≥299
clean pushes is a *guard against silently relaxing the rules*, not a roadmap:
the correct construct is a streak that resets on the first false push, and at
a true 1% false rate the chance of an uninterrupted clean run of 299 is 5% —
about 1,900 pushes, roughly **480 days** at the 4/day cap. Leave it on `rules`.
The axis that *can* move is `g1_model`, because queued items legitimately feed
`queue_worth_rate`; queued items must **not** count toward `clean_pushes`,
which measures what happened when the gate said yes.
[documents/21](documents/21-phase3-findings.md)

**The honest state, since `main.py --evidence` reports it:** exactly one of
these five has a data source in shipped code. `detail_generation` reads
`drill_miss_rate` from the real ledger; the other four need *labels* — a human
or trusted judge saying whether an interruption was correct — and nothing
ships that produces one. `sim/ledger.py` has a `label()` function, called only
from `experiments/soak.py` against a virtual wearer. So "the axes calibrate at
install" is a design intent that is not implemented yet, and `--evidence` says
so in one command rather than leaving it to be discovered.

One metric needed fixing to make that table truthful. A deferred drill used to
be recorded as `drill_miss` — the wearer tapped and got nothing, so it looked
like a miss. But while the axis is off *every* tap is deferred, so the rate
read 1.0 and switched the axis on using evidence manufactured by the deferral
it caused. `drill_miss` (we tried, there was nothing) and `drill_deferred` (we
never tried) are now distinct events, and only the first counts. The cost is a
deliberate deadlock: with the axis off there are no misses to observe, so it
cannot turn itself on from evidence alone. Breaking that needs an override or
a shadow mode — a decision, not a bug.

Design: **[policy.py](policy.py)** · tests: **[experiments/policy_axes.py](experiments/policy_axes.py)** · [documents/20](documents/20-evidence-bridge.md)

---

## Experiments

Everything here is reproducible and every number is computed from a real run.

```bash
.venv/bin/python main.py --selftest            # 50 offline checks, no radio, no API key
.venv/bin/python experiments/glance_gate.py    # glanceability gate + repair
.venv/bin/python experiments/policy_axes.py     # every axis resolves from evidence
.venv/bin/python experiments/store_stress.py    # 400 concurrent writes, zero loss
.venv/bin/python experiments/learning_substrate.py  # does the interest signal survive?
.venv/bin/python experiments/adaptive_loop.py   # no prompt carries the wearer; heartbeat isolation
.venv/bin/python experiments/interrupt_gate.py   # push-vs-queue: silence, budget, ordering
.venv/bin/python experiments/wiring_audit.py     # nothing advertised is left unwired (fails the build)
.venv/bin/python experiments/suspend_gate.py   # stoppable; one mis-fire mutes one class for 24h
.venv/bin/python experiments/link_gate.py      # refuses the one-lens, un-negotiated and silent links
.venv/bin/python main.py --queue --queue-dir .   # dry-run the interrupt decision on a spool
.venv/bin/python main.py --evidence              # which policy metrics are real, and which have no source
.venv/bin/python main.py --kill-switch show      # is pushing armed?
.venv/bin/python main.py --suspend meeting       # mute one source class for 24h (documents/07)

ollaya serve                                   # required for the two below
.venv/bin/python experiments/score_corpus.py   # winnow discrimination over 40 candidates
.venv/bin/python experiments/report-A.py       # -> experiments/results/REPORT-A.md
.venv/bin/python experiments/clock_feature.py  # the falsified clock hypothesis
```

### What the experiments established

| finding | detail |
|---|---|
| The decision model **cannot be the gate** alone | noise-vs-interrupt AUC 0.958; glance-vs-interrupt **0.813** zero-shot. Best precision 0.875 at recall ≥0.50 against a 0.99 budget. |
| Two boundaries, not one | The first is objective; the second is wearer-specific and needs labels. |
| It is **not fooled by tone** | `!!! URGENT !!! read when convenient` → 0.70; `quick one: prod database is down` → 1.73. |
| A clock feature **does not help** | Tested and **falsified**. Every veto variant made it worse. The errors are tier assignment, not time arithmetic. |
| Glanceability: winnow beats a regex | 0.838 vs 0.688, paired bootstrap Δ +0.150 — but winnow verifies *concreteness*, not *glanceability*, and is blind to burial. |
| Gaze has a **cliff** at ρ = θ/A_b ≥ 1.6 | Below it, fabricated glances *contaminate* labels rather than adding noise. |
| **The two-stage LLM split is gone** | `DESIGN.md` claimed one model doing both "degrades the reasoning", never tested. Measured on a 20-item ground-truth corpus: one call is **9–13x faster at identical recall and no less accurate on invented numbers**. End-to-end median **6.71s → 0.48s**. `G1_TWO_STAGE=1` restores it. |
| `winnow` fits alone, cannot share | 5826/6141 MiB resident — 95% of VRAM. |
| `tev1` is unusable | No chat template; rejected by the OpenAI-compatible endpoint. |

Reports: **[REPORT-A.md](experiments/results/REPORT-A.md)** ·
**[REPORT-A2-ordinal.md](experiments/results/REPORT-A2-ordinal.md)** ·
**[clock-feature.md](experiments/results/clock-feature.md)** ·
**[SOAK.md](experiments/results/SOAK.md)**

### The adaptive loop, and what is still unproven

`run_query()` no longer reads the store at all. The injection that consumed it
was measured and removed ([below](#it-does-not-earn-its-place--measured-not-assumed-and-now-removed)),
so the loop today is:

```
tap ──► _record()  ──to_thread──► SQLite (events, cards)
                                  │
                                  ▼
                     topic_evidence()  per-CARD, damped, evidence-gated
                                  │
                                  ▼
                     main.py --weights / prefs        (observation only —
                                                      nothing reads this to
                                                      write a prompt)
```

Four properties are measured, not asserted:

| claim | measured |
|---|---|
| Writes never stall the BLE heartbeat | baseline 11.4ms, **off-loop 11.5ms, inline 20.0ms** — off-loop writes add 0.1ms, inline adds 8.6ms. Measured against a baseline arm, not the nominal interval: asyncio's own overhead puts the real floor at ~11ms. |
| Concurrent writes are durable | 8 threads × 50 writes → **400/400 rows**, 400 distinct ids, 0 orphans. |
| `close()` cannot free a connection in use | pre-fix **segfaults 3/3 runs**; fixed **survives 18/18**. Runs in a subprocess — a segfault cannot be caught in-process. |
| A topic is an interest, not one tap | 4 taps on one card → weight 2.23 but **excluded** from `interests()`; 2+ distinct cards required. |
| Silence stays silence | never-reacted terms sit at exactly neutral with `cards=0`, and adding them does not reorder the ranking. |

```bash
.venv/bin/python main.py --weights   # what the store has learned, and on what evidence
```

**It does not earn its place — measured, not assumed, and now removed.** The A/B
in `experiments/personalisation_ab.py` fired on **0 of 8** ordinary questions,
twice on two independent runs: the salience gate requires the question to
literally contain an interest word, and natural phrasing does not do that. So
on normal traffic the block was never sent at all. Where it did fire (n=3) it
**shifted the polarity of the answer** once — and on the re-run it produced a
small apparent win instead, which is the honest way to say that at n=3 the
conditional arm cannot be read either way. It cost 0.44s vs 0.25s, against a
budget just tightened from 6.71s to 0.48s.

The removal rests on the two rows that reproduced — inert in normal use,
expensive when active — not on the polarity observation, which is single-run.

So the block was **deleted, not switched off**. `--no-personalise` went with
it, because a flag that disables nothing is worse than no flag. Four sites
went: the stage-1 imperative, the gated stage-2 template, the one-call block,
and the per-query history scan that fed them — that scan cost ~26ms of
heartbeat at 8000 events and was paid on every question to build a string the
model was told to ignore. `experiments/adaptive_loop.py` §2 and §3b now assert
that no prompt on either architecture carries a block, against a planted-block
positive control so the absence cannot pass vacuously.

The learning substrate underneath is sound and remains useful for *observing*
the wearer (`--weights`, `prefs`) — but be precise about "sound": after the
removal its topic weights have exactly one consumer in the shipped package,
and that consumer is a human. They do not rank the interruption queue (that
gate reads `queue_worth_rate`) and they reach no prompt. Full account, a
correction to my own first reading of it, and the third injection site that
turned out never to have been wired:
[documents/18](documents/18-personalisation-measured.md).

---

## Simulation

The relay system is downstream of events arriving, and every reviewer agreed
nothing can be calibrated without them. `sim/` generates a plausible day of
them so the pipeline, ledger and review loop can be exercised with no glasses
and no calendar.

```bash
.venv/bin/python -c "
from datetime import datetime
from sim.events import EventGenerator
for c in EventGenerator(seed=1).day(datetime(2026,10,16), count=60):
    print(f'{c.cls:<12} {c.line}')"
```

- **`sim/events.py`** — synthetic candidates across nine classes, with realistic
  hour-of-day weighting. Meetings cluster in working hours, social noise in the
  evening. ~8% of a day's events are push-eligible.
- **`sim/ledger.py`** — the decisions ledger, applying
  **[documents/09-schema.sql](documents/09-schema.sql) verbatim**, so the
  proposed schema is exercised rather than merely written down.
- **`sim/wearer.py`** — a virtual wearer, written by an independent reviewer
  specifically to be adversarial to the design rather than confirmatory.

---

## What is verified, what is not

**Measured on real hardware or a real model:**

- All BLE packet formats, MTU-respecting chunking, name parsing, event decoding
  — 42 offline checks.
- The display budget in characters, and that `page` fields index screens.
- `winnow` latency, VRAM residency, and discrimination over a 40-item labelled
  corpus, including the falsified clock hypothesis.
- Glanceability discrimination against a frozen regex baseline.
- Gait simulation: the ρ cliff and the debounce constants.
- Walking-tier CPU cost; the absence of BlueZ on this host.
- The push-vs-queue decision: silence by default, the budget binding a batch,
  and the ordering, all offline with no radio (`experiments/interrupt_gate.py`).

**Implemented but never exercised on the physical path:**

- The interrupt decision itself. `interrupt.py` is complete, tested and dry-run
  only — **nothing ships that sends a push.** `main.py --queue` prints what it
  would do. The BLE half of an interruption has never run.
- The learning ledger's effect on anything. `drill_miss_rate` is the one policy
  metric with a real source; the other four have none
  ([documents/20](documents/20-evidence-bridge.md)).

**Assumed, never tested — everything about the physical glasses:**

- that `0x02`/`0x03` mean head up/down at all
- that the display is 5 lines × 24 characters
- that MTU negotiation behaves as described
- that text written with `0x4E` shows regardless of head angle
- that a firmware tilt threshold θ exists as modelled

**The number that gates the most is θ.** Nobody has measured it. It is a
ten-minute no-glance walk. Until then, every claim resting on the examination
signal is conditional — which is exactly why `examination_source` defaults to
`touchbar`.

Three earlier claims of this repo were falsified by measurement and the
corrections are kept visible rather than quietly edited:
[documents/README.md](documents/README.md#what-is-decided-vs-still-open)

---

## Documentation index

| # | Document | What it is |
|---|---|---|
| 01 | [Hardware facts and research](documents/01-hardware-facts.md) | Verified protocol facts, measured local models, and the HCI/attention research the design leans on. |
| 02 | [Claude: architecture critique](documents/02-claude-architecture-critique.md) | Adversarial review of the first architecture. Found four defects that were then fixed. |
| 03 | [Design brief](documents/03-design-brief.md) | The brief given to the design panel. |
| 04 | [Design brief: operational](documents/04-design-brief-operational.md) | Same brief plus hosting, latency and field-failure angles. |
| 05 | [Claude: design](documents/05-claude-design.md) | Full design proposal, including a correction to the push/pull premise. |
| 06 | [zcode: operational design](documents/06-zcode-operational-design.md) | Host reality, state machines, latency budget, field failure modes. |
| 07 | [Panel: interaction model](documents/07-panel-interaction.md) | Push budgets, gesture vocabulary, candidate sources, what kills the product. |
| 08 | [Panel: learning substrate](documents/08-panel-learning-substrate.md) | Two-axis signal decomposition, review loop, calibration. All SQL executed. |
| 09 | [Proposed schema](documents/09-schema.sql) | Executable SQLite DDL for the decisions ledger. |
| 10 | [Turn B: glanceability](documents/10-turn-B-glanceability.md) | Can a decision model verify glanceability pre-display? |
| 11 | [Turn A: blocked attempt](documents/11-turn-A-attempt.md) | A reviewer's run that was sandboxed — kept as a record of what could not be measured. |
| 12 | [Turn C: gait and host](documents/12-turn-C-gait-and-host.md) | Gait simulation, the ρ cliff, and the two-tier host split. |
| 13 | [Round 2: Claude convergence](documents/13-round2-claude-convergence.md) | Final design proposal, plus the sharpest argument in the corpus. |
| 14 | [Round 2: zcode convergence](documents/14-round2-zcode-convergence.md) | Final design proposal, partly in disagreement with 13. |
| 15 | [Adversarial finding triage](documents/15-adversarial-finding-triage.md) | What survived an attack, what did not, and what is still open. |
| 16 | [Play-test: the first real run](documents/16-playtest-first-real-run.md) | What happened when the real pipeline met a real model. |
| 17 | [JEV and JEPA: what transfers](documents/17-jev-jepa-transfer.md) | Two independent reviews of SuperInstance's JEV/JEPA work, and what it changes here. |
| 18 | [Personalisation, measured](documents/18-personalisation-measured.md) | The A/B, the verdict, and the removal — including a third injection site that was never wired to anything. |
| 19 | [The wiring audit](documents/19-wiring-audit.md) | What is described as working and is not: the dormant plugin seam, a gate that stopped running, and a decision function that did not exist. |
| 20 | [The evidence bridge](documents/20-evidence-bridge.md) | Why the policy could not change, a metric that measured its own deferrals, and absent-is-not-zero. |
| 21 | [Phase 3 findings](documents/21-phase3-findings.md) | Two bugs that would have shipped, the observability cell, and why the 299-push target is unreachable. |
| 22 | [Verifying an interruption](documents/22-interruption-verification.md) | The literature is mostly empty here; a link drop is censoring, not delay; there is no evidence for a numeric budget. |

The synthesised design is **[DESIGN.md](DESIGN.md)** — read its *Contested*
section before building anything, because those questions became switches.

---

## Repository layout

```
main.py            CLI, session wiring, REPL, offline self-test
g1_driver.py       BLE protocol: arm pairing, 0x4E packets, heartbeat, MTU, TouchBar
pipeline.py        LLM stages, provider presets, glanceability gate, repair
session.py         card lifecycle, drill-down, reaction recording
memory.py          local feedback store (cards, reactions, topic weights)
policy.py          contested decisions as axes with deciding metrics
interrupt.py       push-vs-queue decision; dry-run only, no BLE write
plugins.py         extension seam — NOT BOOTSTRAPPED, see documents/19
sim/               synthetic event source, decisions ledger, virtual wearer
experiments/       reproducible measurements against local models
documents/         design history, including the wrong turns
DESIGN.md          the synthesised design
```

`g1_driver.py` imports on its own and has no LLM dependency. `pipeline.py` has
no BLE dependency. The split is deliberate: each half is testable without the
other.

## Requirements

Python ≥3.9, `bleak` for BLE, `openai` for the LLM pipeline (optional — the BLE
half runs without it). On Linux: BlueZ, and your user in the `bluetooth` group
(`sudo usermod -aG bluetooth $USER`, then log out and back in).