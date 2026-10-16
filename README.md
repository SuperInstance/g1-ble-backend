# g1-ble-backend

A custom developer backend for **Even Realities G1** smart glasses, built from
scratch on [`bleak`](https://github.com/hbldh/bleak). Scan for the glasses,
connect, push text onto the waveguide, and capture TouchBar packets.

Plus a **two-stage LLM pipeline** that compresses a full answer down to the
~25 words that actually fit on the lenses.

No vendor SDK. No app reverse-engineering dependency. Just bleak and the
protocol.

> **Status.** Everything in this repo is verified against a real environment
> *except* the glasses themselves — no G1 hardware was available during
> development. The BLE code paths that need hardware (scan, connect,
> notify, render) are unproven. See [Verification](#verification) for exactly
> what was and was not tested.

---

## Quick start

```bash
./setup.sh
source .venv/bin/activate
python main.py
```

Wear the glasses, make sure they are powered on and **not** bonded to the Even
Realities mobile app, then type a question at the `g1>` prompt.

Point it at a model first — local needs no API key:

```bash
python main.py --list-providers        # what you can connect to
python main.py --provider ollama       # local model, no key required
python main.py --help                  # everything else
```

BLE-only use works with no API key and no LLM configured:

```bash
python main.py --one-shot --text "hello from python"   # send literal text
python main.py --scan-only                            # see what advertises
python main.py --verbose                              # log every BLE write
```

---

## Hardware notes

These are the things that will waste your time if you don't know them up front.

**The G1 is two BLE peripherals, not one.** Each arm has its own radio and is
discovered separately. They advertise as:

```
G1_<channel>_<L|R>_<serial>      e.g.  G1_07_L_S110LAAL103842
```

A single connection reaches one arm only — text appears on half the waveguide.
The driver pairs arms by channel number and writes left-then-right.

**Notifications only flow after MTU negotiation.** Left at the 23-byte minimum
ATT MTU, the glasses stay silent and TouchBar taps never arrive. bleak handles
this automatically on Windows and macOS; on Linux the BlueZ backend needs
bleak's private `_acquire_mtu()`. If taps produce no output on Linux, check the
`MTU=` value in `--verbose` output first.

**The link drops after ~32 seconds without a heartbeat.** A `0x25` heartbeat
task runs for the lifetime of the connection. This is why the interactive
prompt is read on a worker thread: a blocking `input()` would starve the event
loop and kill the link while you think.

**Writing with the wrong mode fails.** The NUS transmit characteristic
advertises inconsistent properties across firmware. The driver reads the
characteristic properties and retries the other mode; `--response` forces one.

---

## The two-stage pipeline

```
query
  │
  ├─[1] generate_primary_response()   heavy model, full long-form answer
  │                                     no length constraint
  ├─[2] condense_for_glasses()        strict HUD filter, ≤ 25 words
  │                                     physical limits, no filler
  └─[3] controller.send_text_packet()  0x4E packet → both arms
```

**Stage 1** does the thinking and writes a normal, complete answer. **Stage 2**
is a structural filter, not a summariser: it is told the display's physical
limits as hard constraints and emits only what fits.

Splitting the roles keeps reasoning quality high and makes display formatting a
separate, cheap, easily-tuned step. Asking one model to do both tends to
degrade the reasoning.

**The word limit is enforced locally, unconditionally.** Stage 2 is instructed
to stay within budget, but its output then passes through
`enforce_word_budget()` anyway — a word count tied to a physical size is not
something to take on faith from a language model. If it overruns, it is
truncated before it ever reaches the BLE stack.

**Failures degrade in place.** A stage-2 failure falls back to deterministic
local truncation of stage 1; a stage-1 failure falls back to a static error
line. The BLE write happens last, so a slow or broken API never delays the
link, and the session survives.

---

## Providers

Any server implementing `POST {base_url}/chat/completions` works. There is
nothing vendor-specific in the code path.

```bash
python main.py --list-providers
```

| Preset | Default endpoint | Key |
|---|---|---|
| `openai` | `api.openai.com` | `OPENAI_API_KEY` |
| `ollama` | `127.0.0.1:11434/v1` | none needed |
| `lmstudio` | `127.0.0.1:1234/v1` | none needed |
| `llamacpp` | `127.0.0.1:8080/v1` | none needed |
| `vllm` | `127.0.0.1:8000/v1` | none needed |
| `openrouter` / `groq` / `together` / `deepseek` | hosted | provider-specific |
| `custom` | whatever you set | — |

Local presets deliberately ship with **no hardcoded model name**, so a fresh
checkout works against whatever you have already pulled — the driver asks the
server what it serves and tells you what it picked:

```bash
python main.py --provider ollama --list-models
#   Models served by http://127.0.0.1:11434/v1:
#     qwen2.5-coder:7b
#     tev1:latest
```

For anything not listed, set the URL directly:

```bash
export OPENAI_BASE_URL=http://some-host:8000/v1
export OPENAI_API_KEY=anything
export OPENAI_MODEL=<model-name>
```

See [`.env.example`](.env.example) for every variable. **No credentials belong
in this repo** — `.env` is gitignored.

---

## Configuration

| Variable | Purpose |
|---|---|
| `G1_PROVIDER` | Provider preset (or `--provider`) |
| `OPENAI_BASE_URL` | Override any preset's endpoint |
| `OPENAI_API_KEY` | Credential; ignored by local servers |
| `G1_PRIMARY_MODEL` | Stage 1 model |
| `G1_COMPRESSOR_MODEL` | Stage 2 model |
| `G1_MAX_DISPLAY_WORDS` | Hard display cap (default 25) |
| `G1_LLM_TIMEOUT` | Per-request seconds (default 30) |
| `G1_LLM_RETRIES` | Retries on transient errors (default 2) |

Useful flags: `--llm-check` (probe the LLM without touching the radio),
`--list-models`, `--dump-services`, `--require-ack`, `--clear`, `--verbose`.

In the REPL: `:help`, `:text`, `:stage1`, `:stats`, `:battery`, `:config`,
`:quit`.

---

## Protocol

Packets use a standard Nordic UART Service:

| UUID | Role |
|---|---|
| `6e400001-…` | NUS service |
| `6e400002-…` | write (host → glasses) |
| `6e400003-…` | notify (glasses → host) |

Text packets are command `0x4E` with a 9-byte header:

```
byte 0   0x4E            command
byte 1   seq             sequence
byte 2   total_packets
byte 3   packet_index
byte 4   screen_status   0x71 = new content + Text Show
byte 5-6 char_pos        16-bit big-endian scroll position
byte 7   page
byte 8   max_page
byte 9+  UTF-8 text
```

TouchBar events arrive as `0xF5 <sub>`. Known sub-commands are decoded in
`TOUCH_EVENT_NAMES`; anything unmapped still prints as raw hex, which is the
point — it's how you map the rest yourself.

All protocol constants live at the top of `g1_driver.py`.

---

## Verification

**Tested and passing:**

- 31 offline checks via `python main.py --selftest` — packet construction,
  UTF-8, MTU-respecting chunking, name parsing, event decoding, pipeline
  handoff, failure containment, word-budget enforcement. No radio, no API key.
- The real `openai` SDK against a mock OpenAI-compatible server: `OPENAI_BASE_URL`
  routing, the two-model split, and the `max_completion_tokens` → `max_tokens`
  legacy fallback.
- The full pipeline against **local Ollama** on an RTX 4050: stage 1 produced
  203 words in 8.4s, stage 2 returned in 0.8s within the word cap.
- Keyless operation: `--provider ollama` with a completely empty environment
  discovers its model and runs both stages.

**Not tested:**

- Everything requiring the physical glasses — scan, connect, MTU negotiation
  in practice, notification delivery, and text rendering. No G1 was available.
- Live cloud API calls.

Treat the protocol constants as a well-sourced hypothesis, not a contract.
They come from the [official demo app](https://github.com/even-realities/EvenDemoApp)
and [community protocol notes](https://github.com/AGiXT/mobile/blob/main/Even%20Realities%20G1%20BLE%20Protocol.txt),
neither of which is an Even Realities API contract.

---

## Requirements

- Python ≥ 3.9
- `bleak` (BLE), `openai` (LLM, optional — BLE half works without it)
- Linux: BlueZ, and your user in the `bluetooth` group
  (`sudo usermod -aG bluetooth $USER`, then log out and back in)

## Layout

```
main.py        CLI, two-stage LLM pipeline, REPL
g1_driver.py   BLE protocol, arm pairing, packet builders, heartbeat
setup.sh       venv + dependency + adapter preflight
```

`g1_driver.py` is importable on its own and has no LLM dependency.