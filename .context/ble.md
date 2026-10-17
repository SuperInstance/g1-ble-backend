# Sector digest: `ble`

> Written by an independent reviewer reading the code, not by the
> agent that wrote it. Point-in-time: 2026-10-16.

## ble

Sources opened: `g1_driver.py` (all 1262 lines), `documents/01-hardware-facts.md`, `documents/02-claude-architecture-critique.md`, plus `DESIGN.md:10`, `README.md:15`, `main.py:887-901`.

### Lead: the three things most likely to waste the next hour
1. **No radio exists on the dev host.** It runs WSL2 with no BlueZ (`DESIGN.md:10`, `README.md:15`). Any scan, connect or MTU work done here fails at the adapter, not in the driver. Get a real BlueZ host (or usbipd passthrough) before you debug any BLE behaviour.
2. **At a low MTU every text send raises `ValueError`, and the selftest passes anyway.**
   - `build_text_packets` only splits *between* wrapped lines (`g1_driver.py:1096-1100`).
   - At MTU 23 the capacity is 11 B, so a 24-char line makes a 33 B packet, which `G1Arm.write` rejects (`g1_driver.py:561`).
   - The MTU-23 test (`main.py:892-901`) checks the header fields only, never the packet length.
   - If MTU negotiation fails, the code logs a warning and leaves mtu=23 (`g1_driver.py:471-475`), and then text fails too.
3. **"Hardware facts" are sourced, not measured.**
   - `01-hardware-facts.md:3` says "Nothing here is a guess", but its §1 protocol table comes from the EvenDemoApp and AGiXT notes.
   - The driver's own docstring calls every constant "a hypothesis to verify" (`g1_driver.py:51-53`).
   - Only §2 (models, latency) was actually measured.

### State
- The driver is complete on paper and has **never touched a G1**. The whole sequence exists in `g1_driver.py:722-1072`: scan → pair by channel → connect L then R → MTU → subscribe → heartbeat → 0x4E text.
- **Measured: nothing on the BLE side.** The offline selftest covers name parsing, the header layout, chunking at MTU 251 and the touch decode (`g1_driver.py:1168-1231`).
- **Assumed (from sources, unverified):**
  - the NUS UUIDs (`:79-83`)
  - the 0x4E header layout (`:253-303`)
  - `0x71` screen status (`:102`)
  - heartbeat = 2 bytes `25 seq` (`:306-308`)
  - the ~32 s link timeout (`:136`)
  - the 0xF5 touch table (`:110-132`)
  - ack = `<cmd> C9|CA` (`:339-351`)
  - battery % at `raw[4]` (`:641`)
  - `0x18` = clear screen (`:1043`, no source cited)
  - ~24 chars × 5 lines (`:1025-1028`)
- The page fields are fixed at 0/1 and the fragment index/count carry reassembly (`:1107-1126`). This is untested on device.
- The **driver does not enforce the display budget**:
  - The wrapped lines are re-joined with spaces (`:1100`), so a sixth line is possible.
  - The critique's pointer `g1_driver.py:1070` is stale; the code is now at `:1091-1100`.
- There is **no reconnect path**:
  - `_on_disconnected` only flips a flag (`:435-437`).
  - `arms` then silently drops the dead arm (`:908-910`), so text keeps going to one arm with no error.

### Open (and the metric that settles each)
- **Does `_acquire_mtu()` give more than 23 on BlueZ?** Check the `connected, MTU=%d` log line (`:418`) for both arms. It passes at ≥ 251.
- **Is the heartbeat format right?** Run 120 s idle with `--hold`.
  - If the link drops at about 32 s, the heartbeat bytes are wrong.
  - Compare against EvenDemoApp's heartbeat builder. I believe it sends a longer frame, but I haven't checked.
- **Does a fixed `seq=0` matter?** Seq is reset on every call (`:1105`), and `G1Arm.next_seq` (`:532`) is never used. Send the same text twice; the second should still render.
- **Does the right arm need the left arm's ack first?**
  - Today there is no per-arm ack: one wait, after both arms, accepting any arm's ack (`:1049-1070`).
  - With `--require-ack --verbose`, count `4E C9` per side.
- **Is triple tap one gesture that alternates 0x04/0x05?** Press it 6 times and log the sub-bytes.
- **Is the screen really 24 chars × 5 lines?** Send 24, 25 and 26-char lines plus a 6-line card, and photograph where each wraps.
- **Is `0x18` really clear-screen?** Run `--clear` followed by short text; the previous text should vanish.

### Traps
- At a small MTU, oversize packets raise before any byte is sent, and the test misses it (`g1_driver.py:1096`, `main.py:895`).
- Glasses advertising only a prefix name ("even g1…") pass `matches_g1` (`:230`) but are then discarded as weak (`:752`), so they never connect.
- `_packet_log` grows without bound (`:705`, `:951`), and `wait_for` re-scans it every 50 ms (`:995-1005`).

### Verify before changing
```
python main.py --selftest                       # offline; covers headers, not packet size
python -c "import g1_driver as g; c=g.EvenG1Controller(); \
  print([len(p) for p in c.build_text_packets('word '*20, mtu=23)])"   # >20 B => bug live
python -c "import bleak.backends.bluezdbus.client as m; \
  print(hasattr(m.BleakClientBlueZDBus,'_acquire_mtu'))"
systemctl status bluetooth; bluetoothctl list
# on hardware only:
python main.py --scan-only --verbose
python main.py --dump-services --verbose
python main.py --text "hello" --require-ack --hold --verbose
python main.py --battery --verbose
```
I didn't run the two `python -c` probes because they needed approval. The MTU-23 claim comes from reading `:1087`, `:1096-1100` and `:561`.

### Falsifiers
- **Any** session on a real G1 does more than all the existing docs. The first scan log confirms or kills the name regex (`:148-151`) and the requirement that both arms are present (`:782`).
- If the link dies at about 30 s despite heartbeats, `build_heartbeat` is wrong (not the interval).
- If no notifications arrive at MTU ≥ 251, the "MTU gates notifications" premise (`:28-33`) is incomplete, for example because an init command is missing.
- If the right arm ignores text when the driver doesn't wait for the left ack, the fan-out (`:1049-1054`) needs per-arm sequencing.
- If a card renders as several screens or not at all, the 0x4E field order (`:290-301`) is wrong.
- If the glasses wrap differently from `textwrap` at 24 chars, the 120-char budget is miscalibrated and should be measured in lines.
