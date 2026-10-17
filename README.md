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
- [Experiments](#experiments)
- [Simulation](#simulation)
- [What is verified, what is not](#what-is-verified-what-is-not)
- [Documentation index](#documentation-index)
- [Repository layout](#repository-layout)

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
        ├─ pipeline.run_query()            pipeline.py   stage 1 reason -> stage 2 compress
        │       │                                        + glanceability gate + repair
        │       v
        │   EvenG1Controller.send_text_packet()         0x4E packet -> BOTH arms
        │       g1_driver.py     scan, connect, MTU, heartbeat, TouchBar capture
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
python main.py --set-policy g1_model=on         # pin one by hand; persists
```

| axis | options | default | settled by |
|---|---|---|---|
| `examination_source` | `touchbar` / `head_up` / `both` | `touchbar` | ρ = θ/A_b ≥ 1.6 from the calibration walk |
| `g1_model` | `on` / `off` | `off` | `queue_worth_rate` ≥ 0.5 over ≥100 review labels |
| `interrupt_gate` | `rules` / `learned` | `rules` | ≥299 clean pushes |
| `detail_generation` | `off` / `on` | `off` | `drill_miss_rate` ≥ 0.30 |
| `push_enabled` | `off` / `on` | `off` | a full fortnight of tier labels |

**Safety property, tested:** with zero evidence every axis holds its
conservative default. An axis cannot be switched on by opinion.

`detail_generation` is already wired into the live path — with the default, a
tap logs `drill_miss` and does not start a generation. That is the point: the
system takes a tap it cannot yet serve, writes down that it happened, and lets
the log decide.

Design: **[policy.py](policy.py)** · tests: **[experiments/policy_axes.py](experiments/policy_axes.py)**

---

## Experiments

Everything here is reproducible and every number is computed from a real run.

```bash
.venv/bin/python main.py --selftest            # 50 offline checks, no radio, no API key
.venv/bin/python experiments/glance_gate.py    # glanceability gate + repair
.venv/bin/python experiments/policy_axes.py     # every axis resolves from evidence
.venv/bin/python experiments/store_stress.py    # 400 concurrent writes, zero loss
.venv/bin/python experiments/learning_substrate.py  # does the interest signal survive?
.venv/bin/python experiments/adaptive_loop.py   # ingestion, gating, heartbeat isolation

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
| Latency claims were ~5–7× optimistic | **CORRECTION.** 110 ms is `winnow`'s number and winnow is **not on the display path**. Measured end to end against a real model (2026-10-16, qwen2.5-coder:7b, RTX 4050): median **6.71 s**, max **19.55 s** — stage 1 alone is 85% of it, at 37.7 tok/s. **Not usable while walking.** See [play-test](#the-wearers-experience-measured). |
| `winnow` fits alone, cannot share | 5826/6141 MiB resident — 95% of VRAM. |
| `tev1` is unusable | No chat template; rejected by the OpenAI-compatible endpoint. |

Reports: **[REPORT-A.md](experiments/results/REPORT-A.md)** ·
**[REPORT-A2-ordinal.md](experiments/results/REPORT-A2-ordinal.md)** ·
**[clock-feature.md](experiments/results/clock-feature.md)** ·
**[SOAK.md](experiments/results/SOAK.md)**

### The adaptive loop, and what is still unproven

`run_query()` reads the store **before** either model runs, so stage 2
compresses against known interests rather than after the fact. The loop:

```
tap ──► _record()  ──to_thread──► SQLite (events, cards)
                                  │
                                  ▼
                     topic_evidence()  per-CARD, damped, evidence-gated
                                  │
                     ┌────────────┴────────────┐
                     ▼                         ▼
        stage-2 prompt (gated)        main.py --weights
```

Four properties are measured, not asserted:

| claim | measured |
|---|---|
| Writes never stall the BLE heartbeat | baseline 11.4ms, **off-loop 11.5ms, inline 20.0ms** — off-loop writes add 0.1ms, inline adds 8.6ms. Measured against a baseline arm, not the nominal interval: asyncio's own overhead puts the real floor at ~11ms. |
| Concurrent writes are durable | 8 threads × 50 writes → **400/400 rows**, 400 distinct ids, 0 orphans. |
| `close()` cannot free a connection in use | pre-fix **segfaults 3/3 runs**; fixed **survives 18/18**. Runs in a subprocess — a segfault cannot be caught in-process. |
| A topic is an interest, not one tap | 4 taps on one card → weight 2.23 but **excluded** from the prompt; 2+ distinct cards required. |
| Silence stays silence | never-reacted terms sit at exactly neutral with `cards=0`, and adding them does not reorder the ranking. |

```bash
.venv/bin/python main.py --weights   # what the prompt is built from, and why
```

**What is not proven.** That this improves anything for a real wearer. The A/B
promised in `.context/llm.md` is still unrun, and stage 2 structurally *cannot*
log which fact it chose, so compliance with the interest rules is unobservable
from the display output. The honest summary: the substrate now measures
cleanly, the injection is gated so it cannot invent facts, and whether it earns
its place is an open question with a flag (`personalise=False`) ready to settle
it.

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
plugins.py         extension seam: role overrides, entry-point discovery
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