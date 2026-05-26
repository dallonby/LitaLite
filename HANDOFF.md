# Handoff — for the next agent

Picking this up after the 2026-05-26 live session that closed out Phases 1–3 of the project. The protocol is now well-enough understood end-to-end to build a working custom client; what remains is targeted decoding of a few side-channels and packaging the scripts as a library. Read [`PROTOCOL.md`](PROTOCOL.md) first — it's the full state of knowledge. This file is just the playbook for what's still open.

## Setup (one-time)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

macOS will prompt for Bluetooth permission for the terminal app the first time `scan.py` runs. Grant it.

If you also want to capture traffic from the official app, install adb (`brew install --cask android-platform-tools`) and follow the HCI-snoop-capture section in `README.md`.

## What's known and verified live (don't re-do)

- Device name prefix: `WDG_Data_<MAC>` (advertising).
- Custom service `00010203-…-0a0b0c0d1910` with two characteristics:
  - `…2b10` — Modbus channel (read + write + notify, ATT handle 0x002A on the LITA-BA)
  - `…2c10` — FF55 event channel (heartbeat + grinder tunnel, ATT handle 0x002D)
- MTU 200, no auth/pairing required.
- Modbus RTU framing, slave `0x01`, CRC16-Modbus (LE on the wire).
- Live telemetry registers (block at 1404+):
  - 1408 = steam boiler temp ×10 °C
  - 1409 = brew boiler temp ×10 °C
  - 1410 = brew pressure ×10 bar (real-time during shot)
  - 1411 = total volume mL
  - 1417 = active pump time s (excludes wait gaps)
  - 1422 = instantaneous flow mL/s **(raw, NOT ×10 — unlike the pressure/temp registers next to it)**
  - 1405 = wall-clock deciseconds since brew start
- Setpoint block at regs 0–36 (configuration, not telemetry).
- Brew control via coil 150 (start), coil 154 (raw valve), coil 155 (clean), reg 87 (active mode 0–4).
- Profile-write sequence: header (7 regs) at slot base, then 6-reg stages with 3-reg gap, then reg 87 ← mode, then coil 150. Captured live and reproduced — see `PROTOCOL.md § 3` for byte-exact transcript.
- FF55 channel carries two framings: opcode-indexed `FF55FFFF<op>` (session, name, etc.) and position-indexed `FF55 02 59 20 00 <len> <payload>` (heartbeat + grinder commands).

If anything above contradicts what you observe, trust observation and update PROTOCOL.md.

## Open work

These are roughly in order of impact ↘ time-cost.

### 1. Decode the FF55 `02 59` heartbeat payload fully

The device pushes a 20-byte status frame every ~500 ms on `…2c10`. We've identified that one byte position carries reg-33's value (steam standby setpoint at 600). The other bytes presumably mirror other selected state. Mapping every byte → register would let a custom client subscribe to the heartbeat and never poll Modbus.

Approach: pull a fresh `record_brew.py` capture that includes `event_hex` per sample, diff successive frames against the polled Modbus state, and identify which byte positions track which registers. Repeat under different conditions (steam wand, brewing, idle) so different field combinations move.

### 2. Identify the single-dose flag in the grinder FF55 frame

Captured grinder write was `ff 55 02 59 20 00 0a 00 00 00 00 00 00 00 4c 02 37 5f`. Of the seven `00` bytes between length and the size/RPM values, one likely carries the single-dose boolean. To pin it down: capture two more grinder writes via the official app — one with single-dose ON, one OFF — and diff. Same trick for any other grinder option visible in the app.

### 3. Map the still-unknown registers in the 1404 block

Most of regs 1404–1423 are idle-zero and didn't move during a brew. Likely fields hiding in there: dispensed weight, current stage index, pre-infusion state, error flags. Approach: run `record_brew.py` for various profile types (weight mode, free variable pressure, single-stage direct extract) and see which previously-zero regs come alive.

### 4. Wider register sweep

We've explored reg blocks 0–36, 387+16, and 1404+20. The verified-fixture reads also touch coils 182+7 and 193+8. There's almost certainly more state in unexplored ranges (e.g. 1500-block is referenced in the JS for "free variable pressure" mode 4). A simple breadth-first sweep — read 16 regs starting at successive addresses through the address space, log which are non-zero — would reveal hot zones.

### 5. Decode the `FF55FFFF` command opcode catalog

We've observed opcodes `0x04, 0x80, 0x81, 0x82, 0x83, 0x87, 0x8B, 0x8C, 0x9A` (see `PROTOCOL.md § 6`). Each has plausible but unverified meanings. Capture more app sessions (login, rename device, factory-reset confirmation flow, OTA?) to fill in the table.

### 6. Wrap into a typed Python library

Currently every script is a standalone CLI. Building `litalite/client.py` with a `LitaLiteClient` class — `connect()`, `read_state()`, `send_profile(profile)`, `brew_and_record(profile)`, etc. — would make this trivial to embed in a UI app. Reuse `modbus.py` (already library-shaped) and the helpers in `send_profile.py` and `record_brew.py`.

### 7. Verify cross-model compatibility

This project was reverse-engineered against a LITA-BA. The app supports `DATA-S`, `LITA-BA`, `LITA-BR`. The `machine_check_lita_*` Dart files suggest the per-model differences are mostly diagnostic-page UI; the wire protocol should be identical. Confirm by running `dump_gatt.py` against a LITA-BR or DATA-S — UUIDs, MTU, register layout should match.

### 8. Confirm reg 1422 flow scaling with a controlled brew

We changed `record_brew.py` on 2026-05-26 to stop dividing reg 1422 by 10, based on a sanity-check of `captures/brew_testyt_full.csv`: a recorded peak of 0.4 mL/s in a shot delivering 68 mL over ~25 s pump-on time (avg ~2.7 mL/s) is physically impossible — peak must be ≥ avg. Removing the /10 puts peak around 4 mL/s, which lines up.

This is an inference from one capture, not a controlled test. To confirm: run a brew with a known total target (e.g. 36 g out in 27 s, target ~1.3 mL/s avg) and check the recorded peak/avg are in the right ballpark. If something is still off, the units may be more exotic (mL per 100 ms? grams per second after density correction?) — diff `volume_ml` per-sample against the integral of `flow_ml_s` to find the actual scale factor.

**Existing CSV captures predating the fix** (`brew_testyt.csv`, `brew_testyt_full.csv`) need a manual ×10 on the `flow_ml_s` column to read correctly. The Crema prototype's CSV loader compensates inline so the bundled captures still render right.

### 9. Steam wand BLE control (unlikely to exist on LITA-BA)

We confirmed the official app has no steam-trigger button and three "diancifa" valve symbols live only in the `project_setting/machine_check` diagnostic page. Steam on LITA-BA is presumed mechanical-only (turn-knob). The LITA-BR has dedicated `machine_check_lita_hot_water_*` variants which *might* mean an additional valve coil exists on that model. Worth checking if you have a LITA-BR available.

## Tools to use

In rough order of usefulness for further reverse-engineering:

- `parse_snoop.py` — the gold standard. Capture the app doing literally anything and decode it byte-by-byte. Section in README.md has setup.
- `record_brew.py` — captures live telemetry + FF55 events to CSV at ~3.4 Hz. Useful for diffing register movements against actions.
- `watch_state.py` — for interactive register-spotting (idle changes, response to physical controls).
- `press_coil.py` / `press_register.py` — for targeted experimental writes.
- `send_profile.py` — sends/replays full profiles. Has `--demo` mode for regression testing against the captured TestyT bytes.

## Safety rules (still apply)

- **Reads are always safe.** Use FC `0x03` and `0x01` to map state before any `0x05` / `0x06` / `0x10`.
- **Never write coil 150 (start brew) without water and a portafilter / drip tray.**
- **Avoid the `FF55FFFF` provisioning opcodes you haven't decoded** — they may include factory-reset / OTA-enter.
- **Don't enter the bootloader** (`AT+RST`) unless you have a known-good firmware image. Bricking is on the table.
- Only one BLE central at a time — force-stop the official app on your phone/tablet before connecting from a script (or vice versa).

## Updating this repo

- When you discover something, edit `PROTOCOL.md` directly. Keep the section layout.
- Cross-check any new packet's CRC with `scripts/modbus.py`'s `crc16_modbus()` before recording.
- Annotate the date and the source of each verified fact (live test vs. static analysis), so future agents can tell what's been observed live.
- The captures directory is gitignored — keep raw snoop logs, bug reports, brew CSVs, and APK extracts local.
