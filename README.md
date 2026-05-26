# LitaLite — Wendougee Espresso BLE Client

An independent BLE client for the Wendougee espresso machine. The official Android app controls it via Bluetooth; this project exists to enable a richer custom client by understanding and re-implementing that protocol.

**Status:** Phase 1 — protocol discovery. Framing fully understood; transport (BLE service/characteristic UUIDs) pending live capture against a real device.

## What's here

- **[`PROTOCOL.md`](PROTOCOL.md)** — everything currently known about the protocol: framing, function codes, register map, OTA, provisioning, verified packet examples.
- **[`HANDOFF.md`](HANDOFF.md)** — what to do next, written for the next agent / contributor working in front of a real machine.
- **`scripts/`** — Python + bleak tooling.
  - `scan.py` — find the device by name pattern `WDGm_<MAC>`
  - `dump_gatt.py` — enumerate services / characteristics / descriptors (this is the missing piece of Phase 1)
  - `listen.py` — subscribe to every notifiable characteristic, log every notification with a timestamp
  - `modbus.py` — packet builder + CRC16-modbus implementation
- **`requirements.txt`** — `bleak` (BLE), nothing else.

## Quickstart

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# find the machine (device advertises as WDGm_<MAC>)
.venv/bin/python scripts/scan.py --filter WDGm

# enumerate its GATT — this gives us the service/char UUIDs we need
.venv/bin/python scripts/dump_gatt.py <address> | tee captures/gatt_initial.txt

# subscribe to notifications and drive the official app on a phone to see live traffic
.venv/bin/python scripts/listen.py <address> --seconds 180 | tee captures/listen_initial.txt
```

## Scope

The user owns this device. This is a personal-use interoperability project; we are not redistributing the manufacturer's code. The extracted APK and decompiled sources stay local — only our own notes, scripts, and protocol facts go in this repo.

## Background

The machine uses **Modbus RTU over BLE** with slave address `0x01`. The official app is a Flutter app using the `universal_ble` plugin; protocol framing logic was recovered from a developer test/debug WebView page bundled in the APK (left in production), and Modbus packets are embedded verbatim in the AOT-compiled Dart binary as constants — providing ~20 pre-CRC'd known-good packets that double as test fixtures. See `PROTOCOL.md`.
