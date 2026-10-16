# Design documents

Ideation and design artifacts for the G1 relay agent, kept in the repo so the
reasoning behind the code is inspectable rather than living in chat history.

These are working documents. Several contain claims that turned out to be wrong
and were later corrected — that is intentional. The record of *how the design
changed* is more useful than a clean final story.

## Reading order

| # | Document | What it is |
|---|---|---|
| 01 | [Hardware facts and research](01-hardware-facts.md) | Verified protocol facts, measured local model performance, and the HCI/attention research the design leans on. |
| 02 | [Claude: architecture critique](02-claude-architecture-critique.md) | Adversarial review of the first architecture. Found four defects that were then fixed. |
| 03 | [Design brief](03-design-brief.md) | The brief given to the design panel. |
| 04 | [Design brief: operational reality](04-design-brief-operational.md) | Same brief plus the hosting, latency and field-failure angle. |
| 05 | [Claude: design](05-claude-design.md) | Full design proposal. Contains a correction to the push/pull premise. |
| 06 | [zcode: operational design](06-zcode-operational-design.md) | Host reality, state machines, latency budget, field failure modes. |
| 07 | [Panel: interaction and event stream](07-panel-interaction.md) | Push budgets, gesture vocabulary, candidate sources, what kills the product. |
| 08 | [Panel: learning substrate](08-panel-learning-substrate.md) | Two-axis signal decomposition, review loop, calibration. |
| 09 | [Proposed schema](09-schema.sql) | Executable SQLite DDL for the decisions ledger. Verified against SQLite 3.46.1. |

## How the panel was run

Four reviewers, deliberately assigned non-overlapping angles so they could not
converge on the same answer:

| Reviewer | Angle |
|---|---|
| Claude Opus | Full design, all open questions; then adversarial critique |
| zcode (GLM) | Operational reality: host, state machine, latency, field failures |
| Subagent 1 | Learning substrate and the implicit-feedback problem |
| Subagent 2 | Interaction model and the candidate event stream |

## What is decided vs. still open

**Settled by evidence:**

- The display budget is measured in characters, not words (5 lines x 24 = 120).
- The G1 is two BLE peripherals; both must be written.
- MTU must be negotiated or notifications never arrive.
- The link drops after ~32s without a heartbeat.
- `0x4E` page fields index screens, not BLE fragments.
- `winnow` reads answer-label logits rather than generating text, and returns
  calibrated-looking distributions (verified on a real run).

**NOT settled — an earlier revision of this file wrongly listed these as
verified. Corrected:**

- *"`winnow:e4b` never fits in VRAM."* 8 GB of model against 6 GB of VRAM
  **suggests** a problem, but what actually happens — partial offload, CPU
  offload, or failure — was never measured. The 22 ms warm figure was one
  observation on one short input, not a benchmark. Being measured now.
- *"The laptop-in-a-backpack is not a viable all-day host."* Strongly argued by
  the operational review (lid-close suspend, thermal throttling, battery) and
  probably right, but not measured on this hardware.
- *"The dev host is WSL2, so no Bluetooth."* The absence of `bluetoothctl` and
  of `org.bluez` on D-Bus was observed directly, but a WSL2 USB-passthrough
  dongle is a plausible fix and has not been tried.

**Agreed across reviewers:**

- Silence is the default state; the drop path is the most important code.
- Nothing can be learned or calibrated until there is a candidate stream.
- Implicit feedback alone does not converge at this volume (tens of events
  per day, one wearer). Explicit daily labels are the primitive.
- The relevance/quality axes must be separate quantities, not one scalar.

**Contested:**

- Whether head-up is an examination signal or a pull request. Claude argues
  the latter and that this is a stronger property; see document 05, point 2.
- Whether the tiered model routing is premature at one user's volume.

**Known blocker:** the development machine is WSL2, which has no Bluetooth
access without passing a USB adapter through with `usbipd`. No BLE path has
been exercised on hardware at all yet.