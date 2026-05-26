# Wendougee BLE Protocol — Reverse-Engineered Notes

Compiled from static analysis of the official Wendougee Android app v3.1.0 (`com.g472631889.stf`). The official app is a Flutter app using the `universal_ble` plugin. Two complementary sources were used:

1. A developer test/debug WebView page (`assets/flutter_assets/assets/web/ble.html`) bundled inside the APK — appears to have been shipped accidentally; contains the protocol layer in plain JavaScript including the full CRC16-Modbus implementation, packet construction, and register addresses.
2. Constant strings inside the AOT-compiled Dart binary (`lib/arm64-v8a/libapp.so`) — contains real, pre-CRC'd Modbus packet hex strings used as defaults / fixtures.

This file is the single source of truth for what we know. Update it as live captures reveal more.

---

## 1. Transport

- **BLE plugin in official app:** [`universal_ble`](https://pub.dev/packages/universal_ble) (Navideck). Generic; doesn't constrain how the device exposes itself.
- **Device name format:** `WDGm_<6-hex-byte MAC suffix>`, e.g. `WDGm_9C9E6E255246`.
- **Service / TX characteristic / RX characteristic UUIDs:** ⚠️ **not yet captured** — these did not appear as plaintext UUID strings in `libapp.so`. They're likely constructed at runtime from 16-bit shorts plus the standard Bluetooth Base UUID `-0000-1000-8000-00805f9b34fb` (which *is* present in the binary). Run `scripts/dump_gatt.py` against a live device to retrieve them.
- **MTU / fragmentation:** undetermined. Modbus control packets are all under 30 bytes so likely fit in a default BLE MTU. OTA YModem packets (130 or 1030 bytes) definitely need MTU negotiation or application-layer fragmentation; mechanism not yet observed.
- **Two distinct packet formats coexist on the link:**
  1. **Modbus RTU** — primary protocol for control and status.
  2. **`FF55FFFF`-prefixed frames** — secondary protocol, used at least for device provisioning (e.g. setting the device name). Less explored.

---

## 2. Modbus RTU framing

```
[ slave_addr (1B) ][ function (1B) ][ ...payload... ][ CRC16-modbus (2B, LE) ]
```

- Slave address is always `0x01`.
- CRC: **CRC16-Modbus** (initial value `0xFFFF`, polynomial reflected, transmitted little-endian — i.e. low byte then high byte). Full byte-table implementation in the original `ble.html`; a clean Python port is in [`scripts/modbus.py`](scripts/modbus.py).
- All multi-byte register values are **big-endian** (Modbus convention).

### Function codes observed

| Code | Meaning | Notes |
|---|---|---|
| `0x01` | Read coils | Layout: `01 01 [addr_hi addr_lo] [qty_hi qty_lo] [CRC]` |
| `0x03` | Read holding registers | Layout: `01 03 [addr_hi addr_lo] [qty_hi qty_lo] [CRC]` — response is `01 03 [bytecount] [data...] [CRC]` |
| `0x05` | Write single coil | Value `FF00` = ON, `0000` = OFF. The app **always** sends both back-to-back as a "press" gesture. |
| `0x06` | Write single holding register | Layout: `01 06 [addr_hi addr_lo] [val_hi val_lo] [CRC]` |
| `0x10` | Write multiple holding registers | Layout: `01 10 [addr_hi addr_lo] [qty_hi qty_lo] [bytecount] [data...] [CRC]` — bytecount = qty × 2 |

### Special control byte sequences (not Modbus, observed in same channel)

| Hex | ASCII | Meaning |
|---|---|---|
| `41 54 2B 52 53 54 0D 0A` | `AT+RST\r\n` | Enter bootloader (OTA) |
| `43` | `C` | Bootloader ready / "send next" |
| `06` | (ACK) | Continue / packet accepted |
| `18 18` | (CAN CAN) | Abort / exit update |
| `01 83 01 80 F0` / `01 83 01 80 F0 43` | — | "Bootloader mode" status response |

---

## 3. Register map (work in progress)

Addresses are **decimal** unless stated. Hex shown for cross-reference.

### Single-register control / status

| Register | Hex | Type | Meaning |
|---:|---|---|---|
| 15 | `0x000F` | holding | Active model/profile selector. Write 0–4 to switch active brew profile. |
| 150 | `0x0096` | coil | **Start brewing** (FF00 = press, 0000 = release) |
| 154 | `0x009A` | coil | Control coil — purpose TBD (likely steam or hot-water dispense) |
| 155 | `0x009B` | coil | Control coil — purpose TBD |
| 157 | `0x009D` | coil | Test-mode coil |
| 1404 | `0x057C` | holding | Model-4 / status block, frequently polled |
| 1441 | `0x05A1` | holding | Single-reg read observed |
| 1459 | `0x05B3` | holding | Additional control register |

### Legacy curve definitions (older profiles, 0–3)

These appear to be a deprecated representation kept for compatibility. The newer per-key blocks (below) supersede them for "quick keys" 1–4.

| Model | Register | Count |
|---|---:|---:|
| 0 | 100 | 7 |
| 1 | 164 | 7 |
| 2 | 228 | 12 |
| 3 | 292 | 12 |
| 4 (free) | 760 | 64 |

### Per-quick-key craft data (active layout, 167 registers each)

| Key | Start register | Length |
|---|---:|---:|
| 1 | 2048 | 167 |
| 2 | 2560 | 167 |
| 3 | 3048 | 167 |
| 4 | 3560 | 167 |

#### Layout within each 167-register block

**Header — 7 regs:**

| Offset | Field | Notes |
|---:|---|---|
| 0 | `callswitch` | 0 = flow mode, 1 = weight mode |
| 1 | `mode` | 0 = constant pressure, 1 = variable pressure |
| 2 | `direct` | 1 = direct extract (single-stage), 0 = multi-stage water |
| 3 | `changeswitch` | 1 = variable flow, 0 = constant flow |
| 4 | `total_flow` | (set if flow mode) |
| 5 | `total_weight` | (set if weight mode) |
| 6 | `auto_link` | linkage flag (e.g. auto-grinder integration) |

**Water stages — N × 6 regs (with 3 reg gap between stages):**

| Offset | Field | Notes |
|---:|---|---|
| 0 | `time` | stage duration |
| 1 | `pressure` | (×10; 0 if priority = flow) |
| 2 | `flow` | (×10; 0 if priority = pressure) |
| 3 | `wait_time` | pause after this stage |
| 4 | `is_end` | 1 = last stage, 0 = continue |
| 5 | `priority` | 0 = pressure-priority, 1 = flow-priority |

### Brew mode taxonomy (`craft.real_mode`)

| Code | Original (zh) | English |
|---:|---|---|
| 0 | 流量恒压 | Flow, constant pressure |
| 1 | 称重恒压 | Weight, constant pressure |
| 2 | 流量变压 | Flow, variable pressure |
| 3 | 称重变压 | Weight, variable pressure |
| 4 | 自由变压 | Free variable pressure |

---

## 4. Verified packet examples

These are real Modbus frames extracted **verbatim** from `libapp.so` as constants. Useful as:
- Initial test transmissions once the BLE TX characteristic is known (the read ones are safe — they don't change anything).
- Test fixtures for the CRC implementation: every CRC here is known-good.

### Reads (safe to send first)

| Hex | Meaning |
|---|---|
| `010100B600079C2E` | Read 7 coils starting at 182 |
| `010100C100086C30` | Read 8 coils starting at 193 |
| `0103000000258411` | Read 37 holding regs from 0 (likely live state dump) |
| `0103057C001484D1` | Read 20 holding regs from 1404 |
| `0103057C00160510` | Read 22 holding regs from 1404 |
| `010301830010B412` | Read 16 holding regs from 387 |
| `010305A10001D524` | Read 1 holding reg from 1441 |

### Writes (require live verification; do NOT send blindly)

| Hex | Meaning |
|---|---|
| `01050096FF006C16` | Coil 150 = ON (start brew press) |
| `0105009600002DE6` | Coil 150 = OFF (release) |
| `0105009AFF00AC15` / `0105009A0000EDE5` | Coil 154 ON / OFF |
| `0105009BFF00FDD5` / `0105009B0000BC25` | Coil 155 ON / OFF |
| `0105009DFF001DD4` / `0105009D00005C24` | Coil 157 ON / OFF (test mode) |
| `0106000F0004B80A` | Reg 15 ← 4 (set active model = 4) |
| `010605B30001B921` / `010605B3000078E1` | Reg 1459 ← 1 / 0 |

---

## 5. OTA firmware update

The bootloader uses **YModem** with XModem-style CRC16 (polynomial `0x1021`, initial 0, big-endian transmit). Packet sizes 128 or 1024 bytes of data, framed as `[FH][seq][~seq][data...][crc_hi][crc_lo]` where FH = `0x01` (128-byte) or `0x02` (1024-byte).

Sequence:
1. Host sends `AT+RST\r\n` to put module in bootloader mode.
2. Device responds with `0x43` ("C") when ready to receive YModem.
3. Host sends YModem start packet (filename + size).
4. After each data packet, device sends `0x06` (ACK).
5. Host finishes with end-packet pair (`04 04 <endpacket>`).
6. `0x1818` cancels / aborts the update.

(Implementation reference: the original `y-modem-send.class.js` shows the exact packet layout.)

---

## 6. `FF55FFFF` provisioning protocol (less explored)

Observed in `libapp.so` as constants. Frame layout (inferred):

```
FF 55 FF FF [opcode (1B)] 00 [length (2B big-endian)] [payload ...] [checksum (1B)]
```

Examples:

| Hex | Decoded payload | Notes |
|---|---|---|
| `FF55FFFF83000100D7` | empty | opcode `0x83`, len 1, payload `00`, csum `D7` — probably a "get" request |
| `FF55FFFF83000101D8` | empty | same but payload `01`, csum `D8` — sequence/ID byte? |
| `FF55FFFF8100115744476D5F39433945364532353532454560` | ASCII `WDGm_9C9E6E255246` | opcode `0x81`, len 0x11 (17), payload is the device name — likely a "set name" command |

Likely used for WiFi credentials provisioning, naming, factory reset, etc. Awaits HCI snoop or live experimentation.

---

## 7. Open questions

- [ ] BLE GATT service / TX char / RX char UUIDs (next live action)
- [ ] BLE MTU and any application-layer fragmentation logic
- [ ] Meaning of registers 154, 155, 1459, and full live-state block (regs 0–37)
- [ ] Complete `FF55FFFF` command catalog
- [ ] Whether the device requires any auth / pairing PIN or accepts any central
- [ ] Response timing — does the device push state changes via notify, or is everything poll-based?
- [ ] How quick-key edits are committed (is there a "save" coil after writing a 167-reg block?)

---

## 8. Sources / tools

- Static analysis tooling: `unzip` + `jadx` + `strings`
- BLE client library used in our scripts: [bleak](https://github.com/hbldh/bleak) (works on macOS / Linux / Windows)
- Optional, for live capture: Android *Bluetooth HCI snoop log* → Wireshark
- Optional, for deeper Dart analysis if needed later: [blutter](https://github.com/worawit/blutter) (Flutter AOT snapshot disassembler)
