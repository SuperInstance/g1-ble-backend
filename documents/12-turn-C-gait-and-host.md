# TURN C — the examination signal, and the host split

Angle: (1) can `0xF5 0x02/0x03` survive being used as an *examination* signal while
the wearer walks, and (2) is the Pi/laptop two-tier split real, and checkable.

Everything below is either a command I ran or a number my code printed.
Simulation source: `/tmp/gait_sim.py` (model + 3 debounce strategies),
`/tmp/gait_sim3.py`, `/tmp/gait_sim4.py` (experiments),
`/tmp/walking_tier_cost.py` (tier CPU cost). Raw output: `/tmp/gait_out4.txt`,
`/tmp/walking_cost.json`, `/tmp/gait_results.json`.

---

# DELIVERABLE 1 — gait simulation and gaze debounce

## 1.1 The model

```
pitch(t) = wander + A_b·sin(2π·(c/2)t) + 0.4·A_b·sin(2π·c t) + glance(t) + noise
```

* `c` = cadence, 1.8–2.0 steps/s. The pitch **nod** rides at stride rate (`c/2`,
  0.9–1.0 Hz); the step-rate component (`c`) is present at 0.4·A_b.
* `A_b` = gait pitch-bob amplitude — **the parameter the brief asks for**, swept 1–8°.
* glance = net lift `G` degrees, 250 ms rise, 1–2 s hold, 250 ms fall.
* wander = 1.0° at 0.15 Hz; noise σ = 0.2°; firmware DLPF = 20 Hz one-pole.
* Firmware model: emits `0x02` crossing above `θ`, `0x03` crossing back down.
  `θ` is the unknown firmware threshold; `ρ = θ/A_b` is what governs everything.

Each condition: 40–60 trials × 180 s = 2–3 walking hours, 6 glances/trial.

**Debounce strategies implemented** (all causal, event-stream-only — the host
never sees pitch):
* **S1 dwell hysteresis** — time-above-threshold accumulator; confirm at `dwell`,
  close only after `release` of continuous below.
* **S2 consecutive-N** — poll the reconstructed `above` flag at 20 Hz, require
  N consecutive true samples.
* **S3 leaky bucket + refractory** — integrates time-above-threshold with a fast
  leak, fires at 1.0, then holds a refractory window.

## 1.2 Closed form: zcode's 300 ms debounce is arithmetically wrong

For a periodic bob, time above threshold is

```
T_above = (1/c) · (1 − (2/π)·asin(θ/A_b))
```

Solving `T_above ≥ dwell` at 1.9 steps/s (computed by the script, printed):

| dwell | gait passes whenever |
|---|---|
| 200 ms | ρ ≥ 0.827 |
| **300 ms** | **ρ ≥ 0.626** |
| 400 ms | ρ ≥ 0.368 |
| 500 ms | ρ ≥ 0.078 |

**Document 06 §5 recommends "300 ms debounce; gait flutter → rejected." That is
false.** A 300 ms dwell is defeated by ordinary walking whenever the firmware
threshold sits within 1.6× of the bob amplitude — a condition nobody has
measured, because the firmware threshold is unknown. At 1.8 steps/s the bob
period is 556 ms and 300 ms of "above" occurs on **56%** of every gait cycle
whenever ρ ≥ 0.626.

## 1.3 The cliff — the measurement that decides the question

A_b = 4.0°, glance lift 22°, dwell 560 ms, 40 trials × 180 s:

| θ | ρ | raw 0x02/h | false/h | onset% | detect% |
|---|---|---|---|---|---|
| 10.0 | 2.50 | 120 | **0.0** | 100 | 100 |
| 8.0 | 2.00 | 120 | **0.0** | 100 | 100 |
| 7.0 | 1.75 | 120 | **0.0** | 100 | 100 |
| 6.5 | 1.62 | 235 | **0.0** | 100 | 100 |
| 6.0 | 1.50 | 580 | 2.0 | 98 | 100 |
| 5.0 | 1.25 | 1404 | 10.5 | 92 | 100 |
| 4.0 | 1.00 | 2670 | 27.0 | 79 | 100 |
| 3.0 | 0.75 | 4154 | 55.5 | 58 | 100 |
| 2.0 | 0.50 | 4529 | 202.0 | 46 | 98 |

120/h is exactly the injected glance count — i.e. **zero gait events** once
ρ ≥ 1.75.

**False-glance rate per real glance** (false / (false + true), with 120 true/h):

| ρ | fabricated fraction of all examinations |
|---|---|
| ≥ 1.62 | **0 %** |
| 1.50 | 1.6 % |
| 1.25 | 8.0 % |
| 1.00 | 18.4 % |
| 0.50 | **63 %** |

**Missed-glance rate is 0 %** everywhere the signal is observable (detect% = 100)
— the gaze event is a *high-recall* signal. The whole problem is precision.

## 1.4 The finding that matters most: every false glance is contaminating

In every single row, `contam/h == false/h`. The contamination metric counts
episodes whose dwell ≥ 250 ms — the floor for "the wearer read something." A gait
crossing that survives a 560 ms debounce is *by construction* long enough to look
exactly like a read.

So there is no cheap false positive. A phantom examination does not add noise to
the examination signal — **it marks whatever candidate is on the lens as READ /
SKIPPED and destroys that candidate's label.** Against E-C Bandit (doc 01 §3),
a contaminated examination is worse than a missing one, because it is
*confidently wrong*. At ρ = 1.25 that is 10.5 corrupted labels per walking hour.

## 1.5 Latency actually added

P50 / P95 of (confirmation − true threshold crossing), θ=6, A_b=4, 50 trials:

| dwell | 300 | 450 | 560 | 700 | 900 | 1200 |
|---|---|---|---|---|---|---|
| p50 | 186 | 279 | 347 | 434 | 558 | 744 |
| p95 | 198 | 297 | 370 | 462 | 594 | 792 |

Latency tracks the dwell at roughly **0.65 × dwell at p50** — the difference is
the onset ambiguity: the head is frequently already above θ when the wearer
decides to look, so part of the dwell is consumed before the glance is real.
**The onset itself is uncertain by up to one dwell period.** A 500 ms debounce
therefore costs ~325 ms p50 / ~370 ms p95, and doc 06's 500 ms head-up→lens
budget is exceeded before a single BLE byte is written.

## 1.6 Strategy head-to-head (θ=6, A_b=4, 3 walking hours each)

| strategy | false/h | detect% | **split%** | p50 lat |
|---|---|---|---|---|
| S1 dwell 300 ms | 1.7 ±1.4 | 100 | **0** | 180 |
| S1 dwell 560 ms | 1.7 ±1.4 | 100 | **0** | 336 |
| S1 dwell 900 ms | 1.7 ±1.4 | 100 | **0** | 540 |
| S2 consecutive-14 @20 Hz | 1.7 ±1.4 | 100 | **0** | 677 |
| S3 bucket, refractory 2200 ms | 3.7 ±2.4 | 100 | **31** | 405 |
| S3 bucket, refractory 4000 ms | 1.7 ±1.4 | 100 | **0** | 405 |

* **S2 beats nothing.** At equal dwell it has identical precision to S1 and
  strictly worse latency, because polling quantises the confirmation to the
  sample grid. It is the same algorithm with a worse constant.
* **S3 with refractory 2200 ms manufactures re-examinations on 31 % of glances.**
  It re-fires mid-hold and emits head-up/head-down/head-up — the exact pattern
  Turn B is building the glanceability predictor on. Push refractory past the
  longest plausible hold (4000 ms) and the splits vanish, at which point S3 is
  just S1 with more state.
* **S1 never splits.** Gait does not manufacture re-examinations under a dwell
  debounce. That is a genuinely good result and it protects the re-examination
  signal the rest of the architecture depends on.

## 1.7 Link corruption

4% of `0xF5` events dropped (body shadowing / retry loss), θ=6:

| strategy | false/h | detect% |
|---|---|---|
| S1 dwell 300 ms | **13.6** | 94 |
| S1 dwell 560 ms | 12.8 | 94 |
| S1 dwell 900 ms | 12.8 | 94 |

4% duplicated: false/h 2.8, detect 100 % — duplicates are harmless, the state
guard absorbs them (confirms zcode's dedup window).

**This is the decisive argument against a short dwell.** Under 4% event loss the
short dwell leaks **13.6 false examinations/hour — 8× the clean rate** — because
a dropped `0x03` welds two gait excursions into one long "above" run. Duplicates
cost nothing. **Loss is the asymmetric failure, and only a long dwell fixes it.**

## 1.8 The wear-signal amplitude floor

θ=6, A_b=4, dwell 560 ms — "net lift" = degrees above his own bob envelope:

| lift G | G−A_b−θ | regime | **detect%** | false/h |
|---|---|---|---|---|
| 4° | −6 | chopped | **3 %** | 0.0 |
| 6° (=θ) | −4 | chopped | **50 %** | 0.0 |
| 7° | −3 | chopped | 76 % | 0.0 |
| 8° (=2·A_b) | −2 | chopped | **92 %** | 0.0 |
| 9° | −1 | chopped | 99 % | 0.0 |
| **10° (=θ+A_b)** | 0 | never-dips | **100 %** | 1.6 |
| 12°+ | >0 | never-dips | 100 % | 1.6 |

**This answers the brief's question directly.** To the brief: *a 3-degree tilt is
indistinguishable from a glance* — measured, a 4° lift is detected 3 % of the
time and a 6° lift is a coin flip.

> **A glance is reliably observable only when the net head lift exceeds θ + A_b.
> Below θ it is 50/50. At 2·A_b it is 92 %.**

Three regimes, all confirmed by the run:
* **G + A_b ≤ θ** — invisible; no event ever fires.
* **G − A_b ≤ θ < G + A_b** — chopped; the hold dips below threshold every bob
  cycle and the dwell resets.
* **G − A_b > θ** — never dips; clean.

## 1.9 What the host can actually measure on day one

`θ` and `A_b` are both unknown and **the host cannot measure either** — it only
receives edges. But it does not need to. The directly observable quantity is the
**gait run-length distribution**: `(next 0x03) − (last 0x02)` during a no-glance
walk.

| A_b | θ | ρ | n runs | p50 | p90 | p99 | **max** |
|---|---|---|---|---|---|---|---|
| 4.0 | 6.0 | 1.50 | 2151 | 20 | 95 | 115 | 125 |
| 4.0 | 5.0 | 1.25 | 4933 | 110 | 175 | 190 | 205 |
| 4.0 | 4.0 | 1.00 | 8342 | 150 | 235 | 250 | 260 |
| 6.0 | 5.0 | 0.83 | 8570 | 205 | 260 | 270 | **275** |

Across every tested ρ, the **longest gait run is 275 ms** — well under one bob
period (500–556 ms), confirming the period bound holds even for multi-component
gait. This is the calibration constant, and it costs one 10-minute walk.

## 1.10 Recommended constants

**Debounce: S1 (dwell hysteresis), `dwell = 500 ms`, `release = 400 ms`.**

* 500 ms sits above every measured gait run (max 275 ms) with 1.8× margin.
* Never go below 500 ms: it is the corrupt-under-link-loss result in §1.7, and
  ρ ≥ 0.626 makes sub-400 ms dwells arithmetically vulnerable.
* `release` slightly under `dwell` (400 < 500) so a real hold is not split by a
  brief gait dip — the split% = 0 result depends on this ordering.
* Total cost: ~325 ms p50 / ~370 ms p95 added latency. **Doc 06's 500 ms
  head-up→lens budget is therefore wrong**; it becomes ~825 ms. Either re-budget
  or accept that the wearer sees text ~0.3 s after he looks up, which is
  probably imperceptible.

**Settle it at install, not in code.** On first wear, run 10 minutes of walking
with no glances, histogram the `0x02→0x03` intervals, and set
`dwell = max(500 ms, 1.5 × p99)`. If p99 > 500 ms, head-up is not an examination
signal and the architecture falls back to the TouchBar. This is a 30-line
addition to M0 and it settles the largest open question in the project.

## 1.11 Verdict — honest answer

**Head-up is usable as an examination signal, but conditionally, and the
condition has never been checked.**

* If **ρ ≥ 1.6** (firmware threshold above the gait envelope): **0.0 false
  examinations per walking hour, 100 % detection, 0 % manufactured
  re-examinations, 0 % missed.** Head-up is a genuinely clean examination signal
  and the whole pull architecture is safe.
* If **ρ ≤ 1.0**: **18–63 % of all detected examinations are fabricated, every
  one of them long enough to be recorded as a real read.** The examination signal
  is worse than useless — it is confidently wrong, which is the failure mode
  E-C Bandit identifies as producing linearly increasing regret.

The disagreement in the panel is therefore not "is head-up an examination signal
vs a pull request." It is **"is ρ above or below 1.6, and nobody knows."** That is
a 30-minute measurement on hardware, and it should be the first thing anyone
does with these glasses. It also replaces the argument: the answer does not
depend on the semantic question at all.

**Two corrections the panel needs:**
1. Document 06's "debounce: collapse gaze toggles under 300 ms" is wrong and will
   manufacture false examinations at ~1.6× threshold-to-bob ratio.
2. Document 06's 500 ms head-up→lens latency budget does not survive any dwell
   debounce worth having.

---

# DELIVERABLE 2 — the two-tier host split, made concrete

## 2.1 What actually exists on this machine

| Check | Command | Result |
|---|---|---|
| OS | `uname -a` / `cat /etc/os-release` | Ubuntu 26.04.1, kernel `6.18.54.1-microsoft-standard-WSL2` |
| CPU / RAM | `nproc`, `free -h` | 24 cores, 15 GiB RAM, AMD Ryzen AI 9 HX 370 |
| GPU | `nvidia-smi` | RTX 4050 Laptop, **6141 MiB VRAM total** |
| ollama | `ollama --version`, `ollama list` | 0.40.5; `qwen2.5-coder:7b` 4.7 GB, `tev1:latest` 4.6 GB |
| ollama residency | `ollama ps` | **empty** — nothing loaded |
| ollaya | `ollaya --version`, `ollaya list` | 0.13.0; `winnow:e4b` 8.0 GB, `laya:en` 853 MB, `laya:multilingual` 683 MB |
| bleak | `bleak 3.0.2`, `dbus-fast 5.2.0` | installed in `.venv` |
| D-Bus | `ls /run/dbus/system_bus_socket` | socket **exists** |
| **BlueZ** | real scan | **`BleakDBusError [org.freedesktop.DBus.Error.ServiceUnknown] The name org.bluez was not provided by any .service files`** |
| BlueZ files | `ls /usr/lib/bluetooth/` | does not exist |
| Bluetooth tools | `which bluetoothctl hciconfig rfkill` | none |
| Adapters | `ls /sys/class/bluetooth/` | no such directory |
| Battery | `cat /sys/class/power_supply/BAT1/power_now` | `0` (on AC; no usable telemetry) |

Two things this box told me that no document says:

1. **A persistent service already holds the GPU.** PID 417 is
   `/home/proart/ml/resident/gpu_resident.py`, an always-on worker. With
   `winnow:e4b` resident this box sits at **5826 / 6141 MiB used, 95 MiB free.**
   There is no room for a second model, and there was not much room before one.
2. **bleak 3.x changed the scanner callback to 2 parameters.** `g1_driver.py:745`
   already matches. A 3-parameter callback fails with
   `TypeError: callback must be callable with 2 parameters` — worth knowing
   before someone "fixes" it back.

## 2.2 What the walking tier actually costs (measured)

`/tmp/walking_tier_cost.py`, run on this host, **no models, no BLE**:

| Work | Cost |
|---|---|
| Rule scoring | 0.34 µs/call → **0.034 %** of one core at 100 candidates/min |
| Gaze ingest (WAL, `synchronous=NORMAL`) | 0.75 µs/event, 1.34 M rows/s |
| A 10-hour walk's worth of gaze rows | 136,800 rows in 0.10 s, 30 bytes/event, 4.0 MB |
| Heartbeat `0x25` packet build | 0.08 µs/beat, 3600 beats/10 h |
| TTL sweep (1/min) | 0.018 ms/tick |
| **TOTAL** | **0.0023 % of one core** → ~0.01 % on a Pi 4 |

The real scorer, `memory.topic_weights()`, scales linearly with history:

| events in 30-day window | cost per call |
|---|---|
| 1,000 | 3.0 ms |
| 10,000 | 26 ms |
| 50,000 | 127 ms |

At one wearer × 30 days (≈1–3 k events) that is 3–9 ms per call. The cliff is at
~50 k events, i.e. ~4,000/day sustained — two orders of magnitude above this
project's volume.

> **The walking tier is not compute-bound in any sense.** Its only real
> constraints are BLE reliability and battery. Zcode's "zero models" rule costs
> nothing in CPU headroom — which also means the absence of models is not
> justified by compute. It is justified by power (§2.4) and by not being able to
> run a GPU in a bag at all.

## 2.3 Exactly what crosses the boundary

**Walks on the Pi (`g1d`):**
BLE central, both arms, MTU negotiation, 10 s heartbeat, candidate queue
lifecycle, TTL sweep, rule scoring (`topic_weights` + kind + recency), gaze
debounce and episode logging, liveness line, packet build and render. **Zero
model calls. Zero network calls. Must be fully correct with the desk tier absent
for a full day.**

**Runs on the desk (RTX 4050):**
`winnow:e4b` shadow scoring of queued candidates; `qwen2.5-coder:7b` generation of
`display_text` (≤120 chars) and `detail_text`; nightly review and label ingestion.

**The boundary is two nullable columns, not a protocol.**

| Direction | Payload | Mechanism |
|---|---|---|
| desk → walk | `candidates` rows | written into the Pi's SQLite **at BOOT and on a 5-min poll**, never a socket the walking tier blocks on |
| desk → walk | `model_score`, `detail_text` | **nullable columns**; NULL never gates anything |
| walk → desk | `exposures`, `gaze` episodes, `health` | same DB file, read after the daemon stops or via SQLite backup API |
| walk → desk | `drill_miss` events | the signal that a candidate needed detail that did not exist |

`NULL detail_text` ⇒ tap shows `· no detail` and logs `drill_miss`. That is the
entire migration story.

## 2.4 Power — what is measured and what is not

**Measured here:** the RTX 4050 idles at **3.73–3.83 W (mean 3.76 W) at 47 °C
merely holding `winnow:e4b` resident**, with zero work in flight. That is the
floor cost of "a model is loaded." I could **not** measure generation power —
the GPU was held at 5826/6141 MiB by a parallel worker's model load for the
entire session, which is itself the finding.

**Not measured, flagged as estimate:** Pi 4/5 class with WiFi and BLE is
~3–7 W from published figures; on a 74 Wh (20 Ah) pack that is **~10–14 h**.
I did not measure a Pi. Treat that figure as published, not verified.

**Not measured, and I decline to claim it:** enclosed-backpack thermals. I have
47 °C idle in an open-air laptop and nothing else. Zcode's thermal-throttle
argument is plausible and unproven here.

## 2.5 Is BLE reliable on Pi-class hardware? Plainly.

Yes, with caveats that are all checkable. Evidence read out of the installed
bleak 3.0.2 source:

* `bluezdbus/manager.py:257` — connects to `BusType.SYSTEM`. Needs a system D-Bus
  and access to `org.bluez`. **I reproduced the exact failure this produces.**
* `manager.py:370 get_default_adapter` — requires an adapter with the BLE
  **`central` role** and `Powered == True`, else raises. On the Pi this is the
  `SetProperty`/polkit path, and a dongle that enumerates unpowered is the
  single most common setup failure.
* `client.py:552 _acquire_mtu` is a **private API**, called directly by
  `g1_driver.py:461`. It picks `AcquireWrite` on the first
  `write-without-response` characteristic, else `AcquireNotify`.
* `client.py:647` — if `_acquire_mtu` is never called, `mtu_size` returns **23**
  with a warning. 23 is exactly doc 01's "notifications never arrive" failure.

So the three concrete BlueZ failure modes are: (1) no `org.bluez` service —
reproduced above; (2) MTU not negotiated → silent, no notifications; (3) adapter
not powered / not central. All three are testable in a startup preflight:

```
assert bluetoothctl show            -> Powered: yes
assert /sys/class/bluetooth/hciX/dev -> present
assert client.mtu_size > 23          -> after _acquire_mtu()
```

Plus BlueZ's own power management will suspend the dongle and kill a link that
already dies after 32 s without a heartbeat: set `Enable=false` on the adapter
and disable USB autosuspend on the dongle via udev. **`bleak`'s BlueZ backend is
not the awkward part — it is the weakest link, because it is a thin D-Bus client
and all three failure modes live in BlueZ's configuration, not in bleak.**

## 2.6 What happens when the desk link drops mid-walk

**Nothing breaks.** That is zcode's strongest point and my measurements support
it: head-up → text is pure BLE plus precomputed bytes, and the whole walking-tier
workload is 0.0023 % of one core with zero models. The drop costs:

| Still works | Stops working |
|---|---|
| Display, gaze logging, debounce, liveness line | New candidates entering the queue |
| TTL expiry, rule scoring of what's queued | `model_score` shadowing |
| Heartbeat, link supervision, health rows | `detail_text` ⇒ tap falls back to the parent line |

The queue is bounded (10 READY, TTL 30 min, doc 06) so a week of desk downtime
degrades to an empty queue plus the liveness line — **toward silence, which is
the correct direction given the stated premise.** And because the liveness line
renders on head-up even when the queue is empty, "the desk tier is down" and
"BLE died in my pocket" remain distinguishable.

**The asymmetry nobody has stated:** the desk tier is not on the *latency* path
but it **is** a hard dependency for the *product*. A day without the desk is a
day of an empty queue — the system degrades to a functioning, silent, useless
thing. That is survivable, and it is still the right architecture; but it should
be written down rather than discovered in week three.

## 2.7 Critical assessment

1. **The split is right. The stated reason is weaker than the real one.** Zcode
   justifies the Pi by battery and thermals — plausible, and I could not measure
   either. The **measured** reason is harder: `winnow:e4b` resident consumes
   **5826 of 6141 MiB**, leaving 95 MiB. The tier split is forced by VRAM
   arithmetic, not by power. No backpack rig fixes that.
2. **Doc 01's 22 ms decision latency is not what a per-candidate call costs.**
   Measured warm whole-invocation: **0.40 s first call, 0.10–0.11 s subsequent**
   via `ollaya run`. Shelling out per candidate costs ~100 ms, not 22 ms. If the
   fast path matters, `ollaya serve` + `/api/decide` exists — but the daemon
   design should assume ~100 ms per decision unless it holds the model resident.
3. **"Zero models on the walking tier" is correct and costs nothing — and is
   therefore not a performance argument.** 0.0023 % of a core leaves enormous
   headroom. The real reason to keep models off the walking tier is battery and
   the fact that a Pi cannot host a GPU model at all. Say that instead.
4. **The desk tier is a single point of failure for content, not for latency.**
   Covered in §2.6.
5. **BLE is still the walking tier's only unmeasured risk, and it is now the
   top priority.** Every number in this document that matters for deployment was
   measured on a box that **cannot run BlueZ at all**. M0 is worth more than doc
   06 claims: one 10-minute walk outside settles BLE reliability *and* the gaze
   regime (§1.11) in the same session.

---

## Numbers I could not produce, and why

| Wanted | Status |
|---|---|
| Generation power draw of the 4050 | GPU held at 5826/6141 MiB by a parallel worker all session |
| Enclosed-backpack thermals | No second machine; declined to claim |
| Pi power / battery life | No Pi attached; estimate flagged as published |
| CPU latency for `laya:en` on the Pi | Not benchmarked; deliberately not claimed |
| The actual firmware threshold θ and real bob amplitude A_b | **No hardware.** This is the central unmeasured quantity in the entire project |