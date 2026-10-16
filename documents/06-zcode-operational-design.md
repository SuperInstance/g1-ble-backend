Read the whole repo. Writing the operational design now — host reality, state machines, latency budgets, the smallest useful build, and field failure modes.

---

# Operational design: the G1 relay as a thing you keep running

**Verdict up front.** Pull is the right model, and it is right for an operational reason stronger than the attention argument: **pull takes every model out of the latency-critical path.** If the queue is precomputed, "head-up → text on lens" is pure BLE, no GPU, no cold start, no VRAM contention. That single property decides the host question, the state machine, and the milestone order below. Conversely, **the laptop-in-backpack is dead on arrival as an all-day host**, and I'll say why with numbers rather than vibes.

Where I flatly disagree with the brief: treating `winnow:e4b` as a walking-hours capability. It is 8 GB of weights against 6 GB of VRAM — it *never fully fits* on the 4050; the verified 22 ms is a warm-cache, partial-offload, short-prompt best case. Budget 200 ms warm and 3–5 s cold for it, and keep it at the desk. The walking host must run with **zero models** and be fully functional that way.

## 1. Host reality

Three candidates, one answer:

| Host | Power | Battery life | BLE | Models | Verdict |
|---|---|---|---|---|---|
| RTX 4050 laptop in backpack | 15–25 W (dGPU awake), more under load | 3–5 h of a 60–90 Wh pack | dies on lid-close suspend unless inhibited | one at a time, cold-swapped | desk only |
| Phone (Termux/native) | 1–2 W | all day | good antenna, but background BLE on Android/iOS means abandoning bleak/Python | none local | the eventual product shape, not this phase |
| Pi 4/5 class SBC in front pocket | 3–7 W with WiFi | ~10 h on a 74 Wh (20 Ah) pack | fine via built-in or dongle | none — by design | **the walking host** |

The laptop fails on three independent axes, any one of which is fatal: lid-close suspend kills BlueZ mid-session; a 4050 in a zipped backpack throttles within minutes (no airflow) and generation drops to a fraction of nominal; and 3–5 h doesn't cover a day. Add the RF problem both laptop and SBC share: glasses on the head, host behind the body in a backpack is the worst case for 2.4 GHz body shadowing. Front pocket or chest strap, and log RSSI from adverts so you can see it.

**Deployment shape — two tiers, one database:**

- **Walking tier:** `g1d` daemon on the Pi. BLE central, candidate queue, pull rendering, gaze logging, TTL sweeps, health. Rule-based scoring only (kind + recency + the keyword weights `memory.py` already computes). It is the single writer to one SQLite file.
- **Desk tier:** the 4050 laptop, SSH'd in during the evening/nightly session. This is where `winnow` shadow-scores candidates, where the 7B generates detail text for tomorrow's queue, and where the nightly review runs. It reads and writes *through* the Pi's file (or a copy-synced copy); it never owns the DB while the daemon runs.

VRAM residency on the desk tier, stated plainly: qwen2.5-coder:7b Q4 is ~4.7 GB weights + ~1 GB KV — it barely fits *alone*; winnow at 8 GB never does. They cannot coexist, so decide statically: **decision model resident, generative loaded on demand.** Rationale: scoring runs on every candidate (background, latency-tolerant), while generation only runs at the desk on demand where a 3–5 s load is acceptable. Also: qwen2.5-**coder** is the wrong generative model for daily-life candidates regardless — but model choice is the learning-substrate reviewer's lane; I only claim the residency policy.

**Degradation ladder** (what dies first when resources run short, in order): generative → model scoring → queue depth (10→3) → earlier PARK. The system degrades toward silence, which is the correct direction given your own premise that silence is the default state.

## 2. State machines

### Link supervisor (`g1d`, daemon-level)

```
BOOT ──config/store ok──► SCAN
SCAN (≤12s) ──both arms──► CONNECTING
SCAN ──nothing──► PARK (30/60/120s, cap 300s) ──► SCAN
CONNECTING (3 tries, 2s/4s backoff) ──≥1 arm──► LINKED (degraded if 1, logged)
CONNECTING ──fail──► PARK
LINKED: heartbeat 10s (g1_driver.py:135), watch disconnects
LINKED ──link drop──► RECONNECT (scan 5s ×2) ──► CONNECTING; 2 fails ──► PARK
LINKED ──case events 0x08/0x0B or removed 0x07 held 60s──► PARK
any ──SIGTERM──► SHUTDOWN (graceful disconnect, existing code path)
```

### Interaction machine (per LINKED session)

| State | Lens | Transitions |
|---|---|---|
| IDLE | clear | head-up → SHOWING(head of queue); empty queue → SHOWING(liveness line, §4) |
| SHOWING | candidate `display_text` | head-down → IDLE + close exposure; tap → DRILLING; double-tap → next candidate or IDLE; SHOW_TTL 90 s no event → IDLE |
| DRILLING | "…" (one packet, instant) | detail ready ≤10 s (GEN_TIMEOUT) → SHOWING(child); timeout → SHOWING(parent) + `drill_miss`; double-tap cancels → SHOWING(parent) |

State-machine guards make duplicate 0xF5 packets idempotent (SHOWING→SHOWING is a no-op). Gait bounce will flutter head-up/head-down; **debounce: collapse gaze toggles under 300 ms into one examination episode.** Log raw events to `gaze`, log *episodes* to exposures. If you skip this, a bouncy walk manufactures fake examinations and poisons the one signal the whole architecture rests on.

### Candidate lifecycle (a row in SQLite)

```
NEW ──score──► READY ──head-up──► SHOWN ──head-down──► READ (dwell ≥1.5s) | SKIPPED
                                     ├──tap──► DRILLED
                                     ├──double-tap──► DISMISSED
                 TTL sweep (created+30min)──► EXPIRED (any pre-SHOWN state)
```

**Crash recovery:** SQLite is already WAL (`memory.py:167`) and the daemon is the single writer, so nothing needs replay beyond state repair at BOOT: `SHOWN → READY` (unknown whether it was seen, unless expired), `NEW/SCORING → re-score or EXPIRED`, queue = `SELECT … WHERE state='READY' ORDER BY priority DESC, id`. In-flight generation tasks are simply lost; the packet log is ephemeral and stays in RAM. One real bug to fix for daemon mode: `controller._packet_log` (g1_driver.py:951) appends forever — cap it at a 10k ring buffer or a multi-day daemon leaks memory.

## 3. Latency budget

Three paths, only one of which is latency-critical — and it contains no model:

| Path | Stages | Cost | Budget |
|---|---|---|---|
| **Head-up → text on lens** | BLE notify 20–60 ms; dispatch ~5 ms; packet build <5 ms (`display_text` is precomputed); 1 packet/arm (120 chars + 9 B header fits one MTU-248 write), 5–15 ms/write + 30 ms pacing (g1_driver.py:1053); render ~50–150 ms | **~150–300 ms** | 500 ms |
| Candidate arrival → READY | rule score <1 ms; winnow shadow 200 ms warm / 3–5 s cold; desk-side generation for detail text 30–60 s on the 7B | seconds | minutes — background |
| Tap → drill detail | SMV: detail precomputed → same as path 1 (~300 ms). Real on-device generation: "…" 300 ms + 3–5 s cold load + 15–30 s for 1200 tokens on a 7B (20–60 tok/s, worse throttled) | 300 ms vs ~30 s | 10 s |

The third row is the honest number nobody wants: **on-device generation while walking is 15–30 s — unusable.** The 10 s GEN_TIMEOUT would fire on almost every drill. That is why the SMV precomputes `detail_text` at insertion (desk model or human) and the walking host does zero generation, ever. Hide cold starts by policy, not by cleverness: models load at desk-session start and never on the walking path; if VRAM contention evicts anything, evict the generative model, keep the decision model resident.

## 4. The smallest useful version

**Goal: two weeks of daily wear producing the evidence that settles pull-vs-push** — head-up rate per hour, % of head-ups that consume a candidate, queue turnover, link uptime, gaze signal fidelity. That evidence, not model quality, is what the architecture question turns on.

In it (~650 lines on top of the existing driver):

1. `daemon.py` (~250): the link supervisor above, systemd user unit, `Restart=on-failure`, `WatchdogSec=60`.
2. `queue.py` (~150): candidate lifecycle, TTL sweep, bounded queue (10 READY; overflow expires silently; nightly digest of expired items is the pressure valve).
3. `pull_ui.py` (~100): interaction machine + gaze debounce + liveness line.
4. `g1ctl` (~80): `add` (inject a candidate with title/display_text/detail_text), `report` (the day: head-ups, exposures, dwell, drops, battery).
5. Schema (~60):

```sql
CREATE TABLE candidates (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  source TEXT NOT NULL,                -- 'inbox'|'calendar'|'manual'
  kind TEXT NOT NULL,                  -- 'reminder'|'info'|'transient'
  title TEXT NOT NULL,                 -- ≤24 chars, the queue line
  display_text TEXT NOT NULL,          -- ≤120 chars, what head-up shows
  detail_text TEXT,                    -- precomputed drill payload
  priority REAL NOT NULL DEFAULT 0,    -- rule score, model-free
  model_score REAL,                    -- winnow shadow score, nullable, never gates
  state TEXT NOT NULL DEFAULT 'new',   -- new|ready|shown|read|skipped|dismissed|drilled|expired
  expires_at REAL, consumed_at REAL);
CREATE TABLE exposures (id INTEGER PRIMARY KEY, candidate_id INT REFERENCES candidates(id),
  shown_at REAL, ended_at REAL, end_reason TEXT, dwell_ms INT, tapped INT DEFAULT 0);
CREATE TABLE gaze (id INTEGER PRIMARY KEY, at REAL, side TEXT, event TEXT);  -- raw, pre-debounce
CREATE TABLE health (at REAL, connected INT, arms TEXT, glasses_batt TEXT, host_batt REAL);
```

(The labeled `decisions` table belongs to the learning-substrate review; `candidates`+`exposures` is what feeds it.)

Explicitly cut: push/interruption entirely (you have not earned it — a week of pull data is what would earn it), on-device generation, winnow in the loop (shadow-scored at the desk only), multi-source plugins, auto-sync, anything clever.

**The single most likely field failure:** in a silence-default system, **a dead link and an empty queue are visually identical.** The wearer stops getting anything and cannot tell "nothing to say" from "BLE died in my pocket" — and because pull makes silence normal, he won't report it. The mandatory countermeasure is the **liveness line**: head-up with an empty queue renders a minimal status line (`· 14:32 3 queued` or just `·`). It costs nothing (one precomputed packet), doubles as a per-examination link probe, and turns "did the system work today" into an answerable question. Without it, your two-week evidence collection silently degrades to "link was down for six hours and we logged head-ups against nothing."

## 5. Field failure modes

| Failure | Why tests miss it | Cheap mitigation |
|---|---|---|
| Adapter drop / BlueZ restart / rfkill | desk sessions are short | supervisor → PARK backoff; udev rule disabling USB autosuspend on the dongle; preflight on every re-init |
| Model OOM (two models, 6 GB) | never both loaded in a test | static residency policy + a semaphore around model calls; on OOM unload generative, keep decision, log to `health` |
| Suspend/sleep killing BLE | laptop open on desk | the walking host is a Pi (no lid); on any laptop, `HandleLidSwitch=ignore` + `systemd-inhibit`. Note: this WSL2 dev box can't run BlueZ BLE at all — deploy target is real Linux |
| Clock skew after resume | `CLOCK_MONOTONIC` stops during suspend | monotonic for intervals, wall clock only for ordering; heartbeat deadlines recomputed after resume; DB ordering by autoincrement, never by timestamp arithmetic |
| Duplicate 0xF5 events (BLE retry) | happens under load, not in demos | (side, hex, ±250 ms) dedup window + idempotent state guards |
| Unbounded growth | queue and `_packet_log` over days | READY cap 10, TTL 30 min, packet-log ring buffer (real bug today), nightly `VACUUM` |
| DB corruption | power cut mid-write | WAL (have), single writer, nightly `PRAGMA integrity_check` + file backup; corrupt → rename and restart (data loss is acceptable at this stage, un-reported loss is not) |
| Thermal throttle in bag | benches run open-air | no GPU on the walking path at all — the architecture *is* the mitigation |
| Glasses battery / in-case | end of every day | case events → PARK; hourly 0x2C battery poll into `health` |
| Gaze flutter (gait) | lab nods are clean | 300 ms debounce; raw logged, episodes derived |

## 6. What to build first, ranked by de-risk per hour

1. **M0 (1–2 evenings): daemonize + `gaze` table + liveness line.** No queue, no models — just the supervisor, full 0xF5 logging with timestamps, and the empty-queue status line. This settles the two unverified facts the *entire* pull thesis rests on: does 0xF5 0x02 actually fire reliably on this firmware while walking, and does the link survive a walk in a pocket? If head-up is flaky or the link dies hourly, everything above changes — and you learn it for ~300 lines of code.
2. **M1 (week 1): queue + pull UI + manual inbox + `g1ctl report`.** Now daily wear produces the consumption evidence.
3. **M2 (week 2): desk-side winnow shadow scoring + precomputed detail text.** Models touch nothing on the walking host; scores and details arrive as data.

If after one week the head-up rate is near zero, pull is dead and you return to thresholded push — with the gaze data to set the threshold honestly. If head-ups are frequent and the queue turns over, the architecture question is settled, and *then* the learning substrate has something real to learn from.
