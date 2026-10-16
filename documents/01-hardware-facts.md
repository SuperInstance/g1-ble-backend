# Hardware facts, measurements, and research

Everything here was verified on this machine or read from a primary source.
Nothing here is a guess.

## 1. Protocol facts (from primary sources)

### The G1 is two BLE peripherals

Each arm carries its own radio and is discovered separately:

```
G1_<channel>_<L|R>_<serial>      e.g.  G1_07_L_S110LAAL103842
```

A single connection reaches one arm only. Text must be written to both, left
then right, per the official demo app.

### Nordic UART Service

| UUID | Role |
|---|---|
| `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | NUS service |
| `6e400002-…` | write (host → glasses) |
| `6e400003-…` | notify (glasses → host) |

NUS is generic. Many unrelated devices advertise it, so name-pattern matches
(`G1_<channel>_<L|R>_<serial>`) are ranked above UUID-only matches.

### Text packet: command `0x4E`

9-byte header + UTF-8 payload:

```
byte 0   0x4E            command
byte 1   seq
byte 2   total_package_num   BLE fragments, for reassembly
byte 3   current_package_num
byte 4   screen_status       0x71 = new content + Text Show
byte 5-6 char_pos            16-bit big-endian
byte 7   current_page_num    SCREENS, not fragments
byte 8   max_page_num
byte 9+  UTF-8 text
```

`page`/`max_page` and `total_package_num`/`current_package_num` are different
fields for different purposes. Conflating them is invisible at MTU 251 and
wrong at MTU 23. See commit `3ce6db0`.

### Constraints that shape everything

- **MTU must be negotiated** or the glasses emit no notifications at all. bleak
  handles this on Windows/macOS; the BlueZ backend needs the private
  `_acquire_mtu()`.
- **The link drops after ~32s without a heartbeat** (`0x25`). A blocking
  `input()` starves the event loop that runs the heartbeat, which kills the
  link — hence `asyncio.to_thread` for terminal reads.
- **The display is ~5 lines x ~24 characters ≈ 120 characters.** Not 25 words:
  25 words is ~199 characters, which renders as 9 lines.

### TouchBar events (`0xF5 <sub>`)

| Sub | Meaning | Bound to |
|---|---|---|
| `0x00` | double tap | `dismiss` |
| `0x01` | single tap | `more` (drill down) |
| `0x02` | head up | *unbound* |
| `0x03` | head down | *unbound* |
| `0x04` / `0x05` | triple tap — **one gesture that alternates bytes per press** | *unbound, deliberately* |
| `0x06` / `0x07` | glasses worn / removed | *unbound* |
| `0x17` | touchbar held | `confirm` |
| `0x18` | touchbar released | *unbound* |
| `0x1E` / `0x1F` | open / close dashboard | *unbound* |

**Heartbeat:** `0x25 + seq`, every 10s (the spec allows 28–30s).

## 2. Measured on this machine

Host: Ubuntu under WSL2, RTX 4050 Laptop (6 GB VRAM), Python 3.14.

### Local models

| Runtime | Model | Size | Notes |
|---|---|---|---|
| ollama | `qwen2.5-coder:7b` | 4.7 GB | |
| ollama | `tev1:latest` | 4.6 GB | |
| ollaya | `winnow:e4b` | 8.0 GB | **decision model** — `choice`/`score`/`noul` |
| ollaya | `laya:en` / `:multilingual` | 683–853 MB | |

**VRAM contention:** 6 GB total. A 7B Q4 generative model (4.7 GB) and the 8 GB
decision model cannot be resident at the same time. This constrains tiering
more than any architectural argument.

### `winnow:e4b` — verified decision output

Never generates text; reads answer-label logits. Real run, `router` preset:

```json
"difficulty":  { "type": "score",  "score": 2.1951, "confidence": 0.4282,
                 "probabilities": {"0":0.0115,"1":0.0996,"2":0.5712,"3":0.3177} },
"domain":      { "type": "choice", "choice": "code", "confidence": 0.9796,
                 "probabilities": {"code":0.983, ...} },
"needs_tools": { "type": "noul",   "noul": 0.229 },
"is_sensitive":{ "type": "noul",   "noul": 0.0477 }
```

Latency: ~22 ms for one warm decision on a short state.

Note `noul` returns a **continuous value**, not a boolean, and `score` returns a
position with a full distribution. Both are useful as *rankings*. Neither is
established as *calibrated* against this wearer's labels — that requires data.

### Pipeline timings (local ollama, `qwen2.5-coder:7b`)

| Stage | Time | Output |
|---|---|---|
| stage 1 (heavy) | 8.4 s | 203 words |
| stage 2 (compress) | 0.8 s | 11 words — **undershoots the 20–25 target** |

The prompt was strengthened to name the band; the local model still compresses
hard. The **maximum** is enforced in code, never the minimum: padding an
11-word answer out to 20 words would make it worse.

## 3. Research the design leans on

**Horvitz — attention as a constrained resource.** A notification system is a
Bayesian decision about whether interrupting will produce enough expected value.
The user's attention is the binding constraint, not the display.

**ProMemAssist** ([arXiv:2507.21378](https://arxiv.org/html/2507.21378v1)) —
smart glasses, working-memory modelling, timing prediction balancing assistance
value against interruption cost. Threshold decision at 0.75 on *their* utility
scale. Closest prior art to this project.

**Interruption cost.** Bailey & Konstan: notifications interrupting a task
cause up to +27% completion time and 2x errors. Delivering at breakpoints cut
frustration 20% and reaction time 25% for ~90s of deferral.

**Glanceability** (UC Berkeley). Peripheral displays needing no focal attention
have near-zero interruption cost; action-requiring notifications do not. The
difference between an ambient display and a notification display is the whole
design space.

**Passive vs active notifications.** Passive (non-blocking) notifications did
not measurably impair flow or performance; active ones produced a larger
performance drop, worst for heavy multitaskers.

**Implicit feedback conflates examination with relevance.** Qi et al., NIPS'18
([E-C Bandit](https://keg.cs.tsinghua.edu.cn/jietang/publications/NIPS18-Qi-et-al-Bandit-Learning-with-Implicit-Feedback.pdf)):
modelling feedback as a composition of *examination* and *relevance* is not
optional — failing to separate them yields **linearly increasing regret**.
Position bias is the single largest distortion: clicks measure past ranker
behaviour at least as much as relevance.

**Cascades and routing.** Confidence self-report is miscalibrated, so cascades
need a verifier or learned deferral. Crucially: *optimal routing often collapses
to simple threshold rules*, and the verifier must be much cheaper than the step
it saves. Also: report the **plateau**, not the argmin — the cost surface is
usually flat, and publishing the argmin is false precision.

## 4. Sources

- [even-realities/EvenDemoApp](https://github.com/even-realities/EvenDemoApp) — official demo, protocol section
- [AGiXT/mobile — G1 BLE Protocol](https://github.com/AGiXT/mobile/blob/main/Even%20Realities%20G1%20BLE%20Protocol.txt) — field-level reverse engineering
- [EldanRing/Winnow-E4B](https://huggingface.co/EldanRing/Winnow-E4B)
- [ollaya winnow docs](https://ollaya.dev/library/winnow)
- [ProMemAssist](https://arxiv.org/html/2507.21378v1)
- [E-C Bandit, NIPS 2018](https://keg.cs.tsinghua.edu.cn/jietang/publications/NIPS18-Qi-et-al-Bandit-Learning-with-Implicit-Feedback.pdf)
- [FrugalGPT cascades](https://arxiv.org/abs/2305.05176)