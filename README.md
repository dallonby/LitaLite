# LitaLite — Wendougee Espresso BLE Client

An independent BLE client for the Wendougee espresso machine (LITA-BA confirmed; LITA-BR / DATA-S likely compatible). The official Android app controls the machine via Bluetooth; this project exists to enable a richer custom client by understanding and re-implementing that protocol.

**Status:** Phases 1–3 complete — protocol is fully reverse-engineered end-to-end.

- ✓ Transport, channel split, framing all confirmed live
- ✓ Live-telemetry register block decoded (pressure, flow rate, total volume, time, dual boiler temps)
- ✓ Brew control commands captured and reproduced (coil 150)
- ✓ Profile-write sequence captured via HCI snoop and replayed successfully — full multi-stage TestyT profile sent + brewed + recorded
- ✓ Grinder-settings tunnel through the machine's FF55 channel decoded

What's left is mostly polish: decoding the rest of the FF55 heartbeat fields, mapping a handful of still-unknown registers, and wrapping the scripts into a typed library for embedding in an app. See [`HANDOFF.md`](HANDOFF.md) for the remaining open items.

## What's here

- **[`PROTOCOL.md`](PROTOCOL.md)** — single source of truth: BLE UUIDs, framing, function codes, register map, live-confirmed packets, FF55 opcodes catalog, full profile-write sequence.
- **[`HANDOFF.md`](HANDOFF.md)** — next steps for an agent continuing the work.
- **`scripts/`** — Python + bleak tooling. Discovery → diagnosis → control:
  - `scan.py` — find the device by name pattern `WDG_Data_<MAC>`
  - `dump_gatt.py` — enumerate services / characteristics / descriptors
  - `discover_endpoints.py` — identify which characteristic is the Modbus channel vs the `FF55…` event channel
  - `listen.py` — subscribe to every notifiable characteristic, log every notification with a timestamp
  - `probe.py` — send a single Modbus frame, accumulate the notify response, validate CRC
  - `watch_state.py` — multi-range live register + coil watcher with change highlighting
  - `press_coil.py` — briefly press a coil ON then OFF while polling state (the way we identified the brew/clean valves)
  - `press_register.py` — same but for holding-register press gestures (1/0)
  - `parse_snoop.py` — parse an Android `btsnoop_hci.log`, decode the ATT writes/notifications and annotate any Modbus / FF55 frames within
  - `send_profile.py` — build + send a multi-stage brew profile to a key slot, optionally fire the shot
  - `record_brew.py` — trigger a brew + record live telemetry to CSV (~3.4 Hz) for graphing / replay
  - `modbus.py` — packet builder + CRC16-Modbus implementation (verified against ~20 known packets)
- **`requirements.txt`** — `bleak` (BLE), nothing else.

## Quickstart

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. find the machine (advertises as WDG_Data_<MAC>)
.venv/bin/python scripts/scan.py --filter WDG

# 2. dump GATT once to confirm the service & char UUIDs match this repo's docs
.venv/bin/python scripts/dump_gatt.py <address>

# 3. live telemetry watcher — change controls and watch which registers move
.venv/bin/python scripts/watch_state.py <address>

# 4. send a profile + brew + record to CSV in one shot
.venv/bin/python scripts/send_profile.py <address> --demo --key 1 --brew
.venv/bin/python scripts/record_brew.py <address> --send-demo --start --out brew.csv
```

The default UUIDs and base addresses are hardcoded based on the live LITA-BA capture and live in `scripts/send_profile.py` constants. Update them if you're targeting a different model.

## Capturing live BLE traffic from the official app

The fastest way to disambiguate any uncertain protocol detail is to capture the official Android app talking to the machine and use `parse_snoop.py` to decode it:

```sh
# On the tablet: Settings → Developer options → "Enable Bluetooth HCI snoop log" → ON, then REBOOT.
# Setup adb-over-wifi, then pull a bugreport (the snoop log is inside it):
adb bugreport bugreport.zip
unzip -j bugreport.zip 'FS/data/misc/bluetooth/logs/btsnoop_hci.log'

# Decode:
.venv/bin/python scripts/parse_snoop.py btsnoop_hci.log
```

## Scope

The user owns this device. This is a personal-use interoperability project; we do not redistribute the manufacturer's code. The extracted APK and decompiled sources stay local (gitignored) — only our own notes, scripts, and protocol facts go in this repo.

## Background

The machine uses **Modbus RTU over BLE** with slave address `0x01`, plus a parallel **`FF55…`-prefixed** channel for events and accessory-tunnelled traffic. Two characteristics on a single custom service:

- `…2b10` carries Modbus (write commands, read replies — full duplex)
- `…2c10` carries FF55 frames (device status heartbeat at ~2 Hz, grinder commands, app session handshake)

Protocol logic was recovered from a developer test/debug WebView page bundled in the APK (left in production), Modbus packets embedded as AOT-compiled Dart constants providing ~20 pre-CRC'd known-good test fixtures, and a live HCI snoop capture of the official app. See `PROTOCOL.md`.
