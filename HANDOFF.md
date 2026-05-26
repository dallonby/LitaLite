# Handoff — for the agent near the espresso machine

You're picking this up from a previous session done on a different machine without physical access to the espresso maker. The static analysis is essentially complete; what's missing is **live BLE confirmation**. Read [`PROTOCOL.md`](PROTOCOL.md) first — that's the full state of knowledge. This file is just the playbook for what to do next.

## Setup (one-time)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

macOS will prompt for Bluetooth permission for the terminal app the first time `scan.py` runs. Grant it.

## Step 1 — Find the device

Power the machine on; make sure the **official app is not connected** (BLE only permits one central at a time).

```sh
.venv/bin/python scripts/scan.py --filter WDGm
```

You should see a device named `WDGm_<hex>`. Capture its address (on macOS this is a CoreBluetooth UUID, not a MAC).

If nothing matches `WDGm`, drop the filter and inspect the full list — the prefix is our best guess from static analysis, but it could be different per model.

## Step 2 — Dump the GATT

```sh
.venv/bin/python scripts/dump_gatt.py <address> | tee captures/gatt_initial.txt
```

This connects, walks every service / characteristic / descriptor, and reads anything readable. Things to record back into `PROTOCOL.md`:

- The **service UUID** the espresso machine exposes (almost certainly one custom service among the standard `0x1800` / `0x1801` GAP/GATT pair).
- The **write / write-without-response characteristic** — this is where Modbus commands go.
- The **notify (or indicate) characteristic** — this is where responses come back.

Common BLE-serial profiles to look out for: Nordic UART (`6e400001-...`), JDY/HM-10 style (`FFE0` service with `FFE1` char), DSD-Tech / generic CYW-based (`FFF0` family). The TX/RX may be the *same* characteristic (write + notify) or two separate ones.

## Step 3 — Confirm framing with a safe read

Pick one of the verified read packets from `PROTOCOL.md` § 4 — `0103000000258411` is a good first try (reads 37 holding registers from 0). Run:

```sh
.venv/bin/python scripts/probe.py <address> --tx <write-char-uuid> --rx <notify-char-uuid> --hex 0103000000258411
```

What to expect:
- Response framing: `01 03 [byte_count] [data...] [crc_lo crc_hi]` — `byte_count` should be `0x4A` (74 = 37 × 2). Total response length 78 bytes. May arrive as multiple BLE notifications; `probe.py` accumulates until CRC checks out.
- If CRC validates, **framing is confirmed** and we can start writing the custom client.
- If the response arrives but CRC is off, suspect a missed fragment or endianness mistake — record raw bytes and update notes.
- If nothing arrives, the TX char might need *write-with-response* instead of *write-without-response*; toggle and retry.

## Step 4 — Watch the official app

While the laptop client stays disconnected, put the phone next to the machine and connect with the official app. **Don't** try to listen and let the app connect at the same time — only one central.

Instead, to observe live traffic:
- **Option A — phone-side HCI snoop** (cleanest): enable *Bluetooth HCI snoop log* in Android Developer Options, drive the app through one representative session (connect, change a setting, pull a shot, change to each key 1–4, run a clean cycle), then pull `/sdcard/btsnoop_hci.log` (or wherever the device puts it; varies by OEM) and open in Wireshark with BTHCI dissector. This yields every byte both directions with timestamps.
- **Option B — disconnect phone, immediately reconnect via laptop** with `scripts/listen.py` subscribed to the notify char, then operate the **machine's physical controls**. Many state changes will be pushed by the firmware. Less complete than (A) but no Android-side setup.

Annotate `PROTOCOL.md` with anything new (registers polled, new opcodes, response timing, key 1–4 register confirmations).

## Step 5 — Build the custom client surface

Once framing + transport are confirmed, the custom app's BLE layer is straightforward:

```python
from scripts.modbus import build_read_holding, build_write_register, parse_response

# read live state
tx_bytes = build_read_holding(slave=0x01, addr=0, qty=37)
# ... write to TX char, await notify, accumulate fragments, parse_response()
```

The hard part of phase 1 is over. The remaining work is filling in the register-map gaps by experiment.

## Safety rules

- **Reads first, always.** Use `0x03` and `0x01` to map state before touching `0x05` / `0x06` / `0x10`.
- **Never write coil 150 (start brew) without water and a portafilter.** Or the equivalent state where a brew can actually run safely.
- **Avoid the `FF55FFFF` provisioning frames** until we understand them — they may include factory-reset opcodes.
- **Don't enter the bootloader** (`AT+RST`) unless we have a known-good firmware image. Bricking is on the table here.

## Updating this repo

When you discover something:
- Edit `PROTOCOL.md` directly. Keep the same section layout.
- Add new verified packets to § 4 with a one-line description.
- Cross-check CRCs with `scripts/modbus.py`'s `crc16_modbus()` before recording.
- Commit with descriptive messages so the off-site agent can follow what changed.
