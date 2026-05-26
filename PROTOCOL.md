# Wendougee BLE Protocol — Reverse-Engineered Notes

Compiled from static analysis of the official Wendougee Android app v3.1.0 (`com.g472631889.stf`). The official app is a Flutter app using the `universal_ble` plugin. Two complementary sources were used:

1. A developer test/debug WebView page (`assets/flutter_assets/assets/web/ble.html`) bundled inside the APK — appears to have been shipped accidentally; contains the protocol layer in plain JavaScript including the full CRC16-Modbus implementation, packet construction, and register addresses.
2. Constant strings inside the AOT-compiled Dart binary (`lib/arm64-v8a/libapp.so`) — contains real, pre-CRC'd Modbus packet hex strings used as defaults / fixtures.

This file is the single source of truth for what we know. Update it as live captures reveal more.

---

## 1. Transport

- **BLE plugin in official app:** [`universal_ble`](https://pub.dev/packages/universal_ble) (Navideck). Generic; doesn't constrain how the device exposes itself.
- **Device name format:** `WDG_Data_<6-hex-byte MAC suffix>`, e.g. `WDG_Data_AABBCCDDEEFF`. (Earlier static-analysis guess of `WDGm_…` was wrong — confirmed from live advertising.)
- **GATT layout (confirmed live, 2026-05-26):**

  | UUID | Role | Properties |
  |---|---|---|
  | `00010203-0405-0607-0809-0a0b0c0d1910` | Custom service (single, only non-standard service exposed) | — |
  | `00010203-0405-0607-0809-0a0b0c0d2b10` | **Modbus channel** — write Modbus frames here, responses arrive on the same char via notify | read, write-without-response, notify |
  | `00010203-0405-0607-0809-0a0b0c0d2c10` | **`FF55…` event/status channel** — device pushes status heartbeats here unsolicited (~2 Hz); writes here don't produce Modbus replies | read, write-without-response, notify |

  Both characteristics have CCCD (`0x2902`) descriptors; subscribe before reading. The device accepts any central — no pairing, no PIN, no auth handshake observed.
- **MTU:** **200 bytes** negotiated automatically on macOS. Modbus control packets (<30 B) fit trivially. YModem 128-byte data packets (132 B framed) also fit. YModem 1024-byte packets still require application-layer fragmentation; mechanism not yet observed.
- **Two distinct packet formats coexist on the link, on separate characteristics:**
  1. **Modbus RTU** on `…2b10` — primary protocol for control and status.
  2. **`FF55…`-prefixed frames** on `…2c10` — used for device provisioning *and* for unsolicited status broadcasts from the device.

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
| 150 | `0x0096` | coil | **Start brewing** (programmed shot, FF00 = press, 0000 = release) |
| 154 | `0x009A` | coil | **Brew start** (latch trigger; fires the full configured shot). Confirmed live 2026-05-26 — pressing it caused 1410/1411/1417 etc. to evolve like a normal brew. |
| 155 | `0x009B` | coil | **Cleaning / group-head flush** — water out the group head, no brew curve. Confirmed live 2026-05-26. |
| 157 | `0x009D` | coil | Pressed live 2026-05-26 — **no observable physical effect**. Possibly a service/test-mode flag or a no-op on the LITA-BA. |
| 1459 | `0x05B3` | holding | Pressed via 1/0 write live 2026-05-26 — **no observable physical effect**. Purpose still unknown. |
| 1404 | `0x057C` | holding | Live telemetry block start (see § 3a) |
| 1408 | `0x0580` | holding | **Steam boiler temp (live, ×10 °C)** — confirmed by steam-wand actuation 2026-05-26: 1255 → 1230 during 5s steam burst, recovered to 1263. |
| 1409 | `0x0581` | holding | **Brew/group boiler temp (live, ×10 °C)** — confirmed 2026-05-26: ~925 idle, climbs to 946 when heater fires alongside steam-boiler recovery. |
| 1441 | `0x05A1` | holding | Single-reg read observed |
| 1459 | `0x05B3` | holding | Additional control register |

### Setpoint / standby block (regs 0–36) — *configuration, not telemetry*

Confirmed 2026-05-26 by polling for 50 seconds while actuating the steam wand: no register in this block moved at all. Earlier hypothesis ("live state dump") was wrong; this block holds setpoints + standby thresholds that are constant during operation. Notable values from a single read:

| Reg | Value | Likely meaning |
|---:|---:|---|
| 2 | 3 | active brew mode (`craft.real_mode` — see § 3 taxonomy; 3 = Weight, variable pressure) |
| 14 | 695 (= 69.5 °C ×10) | likely a standby/idle setpoint (Dart: `boilStandbyTemperature`?) |
| 33 | 600 (= 60.0 °C ×10) | likely a standby/idle setpoint (Dart: `steamStandbyTemperature`?) |
| 15 | 0 | active model selector |

### 3a. Live telemetry block — reg 1404+

Live values captured during a manual-M shot 2026-05-26 (~11 second pump-on). Registers that moved:

| Reg | Hex | Idle | During brew | Post-brew | Meaning |
|---:|---|---:|---|---:|---|
| 1405 | `0x057D` | 0 | linear ramp, **not capped at 100** (observed reaching 148 during a 14.6s recording) | last value held | **Elapsed brew time in deciseconds** (1/10 s) — earlier "progress %" hypothesis was wrong, confirmed 2026-05-26 from a live multi-stage shot |
| 1408 | `0x0580` | ~1255 | dipped 1255→1250 | recovered | **Steam boiler temp ×10 °C** (confirmed) |
| 1409 | `0x0581` | ~925 | climbed 925→935 (heater fired) | settling | **Brew boiler temp ×10 °C** (confirmed) |
| 1410 | `0x0582` | 0 | jumped to 88, held 86-88 throughout | 0 | **Brew pressure ×10 bar** (~8.8 bar — typical) — drops to 0 at shot end |
| 1411 | `0x0583` | 0 | linear ramp 0→46 | 46 (held) | **Flow volume (mL)** ✓ user-confirmed 2026-05-26 |
| 1417 | `0x0589` | 0 | linear ramp 0→15 | 15 (held) | **Shot time (s)** — matches 15 s shot duration |
| 1422 | `0x058E` | 0 | brief 4-5, back to 0 mid-shot | 0 | **Instantaneous flow rate (mL/s)** — matches 1411/elapsed avg ~4.2 mL/s |
| 1404, 1406-1407, 1412-1416, 1418-1421, 1423 | — | 0 | did not move | 0 | unknown, idle-zero (possibly weight, pre-infusion phase, etc.) |

### 3b. Other holding-register block — reg 387+

| Reg | Hex | Value observed | Likely meaning |
|---:|---|---:|---|
| 397 | `0x018D` | 1 | flag |
| 398 | `0x018E` | 135 | — |
| 399 | `0x018F` | 30000 | likely `alarmExtractionTimeout` (ms) |
| 400 | `0x0190` | 1 | flag |
| 401 | `0x0191` | 150 | likely brew-time cap (s) |

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

#### Write protocol (decoded from `ble.html` `getItemShortcutKeyCommond`, 2026-05-26)

A profile is **not** written as one 167-reg Modbus call. It's a small series of `0x10` writes scattered through the slot, with deliberate gaps:

```
register = base                         # 2048, 2560, 3048, or 3560

# 1. Header (7 regs) ─ ble_writes(register, 7, header_data)
register += 7 + 1                       # 1-reg gap after header

# 2. Stages
if direct:                              # single-stage
    ble_writes(register, 5, [0, 0, flow*10, 0, 1])
    register += 5 + 3                   # advance by 8 (single-stage gap is 3)
else:                                   # multi-stage
    for each stage:
        ble_writes(register, 6, stage_data)
        register += 6 + 3               # 3-reg gap between stages
```

So per profile you get **1 header write + N stage writes** — each well under MTU. No need to ever write the whole 167-reg block.

#### Header layout — 7 regs

| Offset | Field | Encoding |
|---:|---|---|
| 0 | `callswitch` | **Inverted in JS**: `weight_mode ? 0 : 1`. So 0 = weight mode, 1 = flow mode (note: JS code's variable name is misleading) |
| 1 | `mode` | 0 = constant pressure (`craft.mode == 2`), 1 = variable pressure (`craft.mode == 1`) |
| 2 | `direct` | **Inverted in JS**: `direct_extract ? 0 : 1`. So 0 = direct/single-stage, 1 = multi-stage |
| 3 | `changeswitch` | 1 = variable flow, 0 = constant flow |
| 4 | `total_flow` | mL (set only if flow mode, else 0) |
| 5 | `total_weight` | g (set only if weight mode, else 0) |
| 6 | `auto_link` | linkage flag (e.g. auto-grinder integration) |

#### Stage layout — 6 regs per stage (multi-stage)

| Offset | Field | Notes |
|---:|---|---|
| 0 | `time` | stage duration (seconds, no scaling) |
| 1 | `pressure` | ×10 bar. **0 if priority = flow** (else stage's `bar.value`) |
| 2 | `flow` | ×10 mL/s. **0 if priority = pressure** (else stage's `flow.value × 10`). For constant-flow mode it's just `craft.flow × 10` regardless. |
| 3 | `wait_time` | pause after this stage; 0 for the last stage |
| 4 | `is_end` | 1 = last stage, 0 = continue |
| 5 | `priority` | 0 = pressure-priority, 1 = flow-priority. Constant-pressure mode writes 0 here. |

#### Direct (single-stage) layout — 5 regs

```
[0, 0, flow*10, 0, 1]
```
Just `flow × 10` and the end-flag.

#### Free variable pressure (mode 3) — different path entirely

When `craft.mode == 3` ("自由变压"):
- Writes 8 separate datasets at the same base register, each `num = 64` regs long, advancing `register += 64` each time.
- After dataset index 7, `register = 1500` (jumps out of the slot).
- Then single-reg writes to `reg 79`, `reg 87`, `reg 358 + (keyNum-1)`, `reg 362 + (keyNum-1)`, `reg 366 + (keyNum-1)`.

#### Activation sequence

After writing the profile registers:
```
0x06 write reg 87 ← real_mode         # active mode selector (0-4 per § 3 taxonomy)
0x05 coil 150 FF00                    # "press" start brew button
0x05 coil 150 0000                    # "release"
```

To just *save* a profile without brewing, skip the coil-150 press pair.

**Note:** earlier PROTOCOL.md said `ble_write(15, real_mode)` based on the legacy JS path in `model_make()`. **The live app actually writes reg 87, not reg 15** — confirmed by HCI snoop on 2026-05-26. Reg 15 is the legacy register, kept around in the binary but unused for the modern quick-key flow. Use reg 87.

#### Full live-confirmed sequence (HCI snoop 2026-05-26)

Captured the official app saving + brewing a 4-stage profile to key 1 (Flow variable pressure mode, total flow 68 mL):

| Time offset | Bytes (on the wire) | Decoded |
|---:|---|---|
| 0 ms | `01 10 08 00 00 07 0e 00 01 00 01 00 01 00 01 00 44 00 00 00 00 0c 2c` | header to reg 2048: `[1,1,1,1,68,0,0]` |
| +51 | `01 10 08 08 00 06 0c 00 07 00 3d 00 00 00 08 00 00 00 00 7d f7` | stage 1 to reg 2056: `[7,61,0,8,0,0]` |
| +108 | `01 10 08 11 00 06 0c 00 03 00 17 00 00 00 00 00 00 00 00 a5 fd` | stage 2 to reg 2065: `[3,23,0,0,0,0]` |
| +159 | `01 10 08 1a 00 06 0c 00 14 00 00 00 11 00 01 00 00 00 01 ba 8f` | stage 3 to reg 2074: `[20,0,17,1,0,1]` |
| +209 | `01 10 08 23 00 06 0c 00 04 00 0b 00 00 00 00 00 01 00 00 e3 fc` | stage 4 to reg 2083: `[4,11,0,0,1,0]` |
| +260 | `01 06 00 57 00 02 b9 db` | reg 87 ← `2` (mode = Flow variable pressure) |
| +352 | `01 05 00 96 ff 00 6c 16` | coil 150 ON  (= start brew) |
| +395 | `01 05 00 96 00 00 2d e6` | coil 150 OFF (= release) |

Total: 7 Modbus writes spanning ~400 ms. Each write echoes a normal Modbus RTU response (`01 10 [addr] [qty] [crc]`) on the same characteristic.

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
| `0103000000258411` | Read 37 holding regs from 0 (likely live state dump) ✓ **verified live 2026-05-26** |
| `0103057C001484D1` | Read 20 holding regs from 1404 |
| `0103057C00160510` | Read 22 holding regs from 1404 |
| `010301830010B412` | Read 16 holding regs from 387 |
| `010305A10001D524` | Read 1 holding reg from 1441 |

### Live state sample — `0103000000258411` response captured 2026-05-26

Sent: `01 03 00 00 00 25 84 11` to `…2b10`
Got back (single 79-byte BLE notification on `…2b10`, CRC valid):
```
01 03 4A 00 32 00 32 00 03 00 00 00 00 00 01 00 00 00 00 00 7D 00 5C
      00 00 00 00 00 00 00 00 02 B7 00 00 00 01 00 64 00 5A 00 5A 00 00
      00 00 00 01 00 00 00 01 00 01 00 01 00 00 00 01 00 00 00 01 00 00
      00 00 02 58 00 32 00 32 00 00 D4 8E
```

Decoded as 37 holding registers (decimal):

| Reg | Value | Hex | Hypothesis |
|---:|---:|---|---|
| 0 | 50 | 0x0032 | — |
| 1 | 50 | 0x0032 | — |
| 2 | 3 | 0x0003 | active brew mode (`craft.real_mode` 3 = 称重变压 / Weight, variable pressure) |
| 3 | 0 | 0x0000 | — |
| 4 | 0 | 0x0000 | — |
| 5 | 1 | 0x0001 | — |
| 6 | 0 | 0x0000 | — |
| 7 | 0 | 0x0000 | — |
| 8 | 125 | 0x007D | — |
| 9 | 92 | 0x005C | — |
| 10..13 | 0 | — | — |
| 14 | 695 | 0x02B7 | **likely group-head or steam-boiler temp ×10 → 69.5 °C** |
| 15 | 0 | 0x0000 | active model selector (PROTOCOL says this; currently 0) |
| 16 | 1 | 0x0001 | — |
| 17 | 100 | 0x0064 | — |
| 18 | 90 | 0x005A | — |
| 19 | 90 | 0x005A | — |
| 20..21 | 0 | — | — |
| 22 | 1 | 0x0001 | — |
| 23 | 0 | 0x0000 | — |
| 24 | 1 | 0x0001 | — |
| 25 | 1 | 0x0001 | — |
| 26 | 1 | 0x0001 | — |
| 27 | 0 | 0x0000 | — |
| 28 | 1 | 0x0001 | — |
| 29 | 0 | 0x0000 | — |
| 30 | 1 | 0x0001 | — |
| 31 | 0 | 0x0000 | — |
| 32 | 0 | 0x0000 | — |
| 33 | 600 | 0x0258 | **likely second temp ×10 → 60.0 °C** (also appears in the `FF55…` heartbeat) |
| 34 | 50 | 0x0032 | — |
| 35 | 50 | 0x0032 | — |
| 36 | 0 | 0x0000 | — |

Per-register meanings to be confirmed by toggling controls and re-reading; see § 7.

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

## 6. `FF55…` event / provisioning channel (`…2c10`)

This channel carries two framing variants:

**Variant A — `FF55FFFF<op>` (host→device commands & device events, opcode-indexed):**

```
FF 55 FF FF [opcode (1B)] 00 [length (1B)] [payload ...] [checksum (1B)]
```

Note: the length field is **1 byte**, not 2 — earlier inference of a 2-byte big-endian length was wrong. The byte after `00` is the length.

| Opcode | Direction | Payload | Meaning |
|---:|---|---|---|
| `0x04` | host→dev | ASCII session token, e.g. `"AB07116957"` | App-side auth/session identifier sent on connect |
| `0x80` | host→dev | ASCII device name, e.g. `"&lt;bound device name&gt;"` | Pushes the bound device-name string |
| `0x81` | host→dev | ASCII device name | Set-name (from `libapp.so` constants, original guess) |
| `0x82` | host→dev | 1 byte boolean (`01`) | Set some flag |
| `0x83` | host→dev | 1 byte (`00`/`01`) | Get-request (from constants; original guess) |
| `0x87` | host→dev | ASCII device name | Bond-confirm name push (same payload as 0x80) |
| `0x8B` | host→dev | 1 byte (`00`/`01`) | Boolean poll / toggle |
| `0x8C` | both | 1 byte query / N bytes response | Get/set device name pair — request payload `00`/`01`/`02`, response carries ASCII name |
| `0x9A` | dev→host | 1 byte (`01`) | One-shot event emitted on first Modbus activity after connect — see live 2026-05-26 |

**Variant B — `FF55<seq><type>…` (data frame, used both directions, observed live):**

Same 6-byte fixed header used in both directions:

```
FF 55 02 59 20 00 <len> <payload bytes> <csum>
```

- `<len>` is the count of payload bytes including the trailing csum byte.
- The first 6 bytes (`FF 55 02 59 20 00`) are constant across all captured Variant-B frames.

**Device → host** (status heartbeat, ~500 ms cadence while connected):
```
FF 55 02 59 20 00 0C 00 36 00 03 00 11 02 58 00 0B 03 84 12
                  ↑len=12                  ↑0x0258=600 (reg 33 / steam standby setpoint)
                                                        ↑0x0384=900 (likely a setpoint)
```

**Host → device** (grinder settings, single observed example):
```
FF 55 02 59 20 00 0A 00 00 00 00 00 00 00 4C 02 37 5F
                  ↑len=10  ↑7 unknowns     ↑76µm ↑567RPM ↑csum
                            (single-dose flag lives somewhere in here)
```

The byte structure suggests Variant-B frames carry tuples of settings/state in a fixed schema indexed by position, not by opcode. Position-to-meaning mapping in heartbeat needs further work.

Both variants are used at least for WiFi credentials provisioning, naming, factory reset, and live state push. The device pushes Variant-B heartbeats unsolicited — Modbus polling is **not strictly required** for status display.

---

## 6b. App architecture (from libapp.so symbol mining, 2026-05-26)

Dart AOT keeps class names and getter/setter symbols, so we can recover structural intent even though top-level constants (e.g. `kBoilerTempReg = 14`) are stripped. The most useful findings:

- **Device modules:** `wendougee_module_device` is the primary BLE/control module; `wendougee_module_device_simple` is a stripped-down/companion variant.
- **Dual-boiler architecture confirmed:** `boilStandbyTemperature` and `steamStandbyTemperature` are distinct fields, and `machineConfigSingleBoiler` / `machineConfigDoubleBoiler` are runtime-detected. → reg 14 (`695` / 69.5 °C) and reg 33 (`600` / 60.0 °C) are almost certainly these two boilers.
- **PID configuration is exposed:** `machineConfigPressureKP`, `machineConfigPressureKI`, `settingFlowVelocityKP`, `settingFlowVelocityKI`.
- **Per-channel calibration offsets:** `settingFlowCompensation`, `settingPressureCompensation`, `settingSteamCompensation` (two: `getSteamCompensation1`, `getSteamCompensation2`), `settingTemperatureCompensation`.
- **Setting endpoints visible as getters:** `settingMPressure`, `settingInputWeight`, `settingTemperatureC` / `…F` / `…Unit`, `settingWaterChangeTemperature`, `settingLeverPressure`, `settingPressureCompensation`, `setLeverPressure`, `standbyTime`, `standbySpace`.
- **Alarm codes** (likely a status bitfield or coil block somewhere): `alarmNTC1`, `alarmNTC2`, `alarmNoPressure`, `alarmHeatingTimeout`, `alarmWaterShortage`, `alarmWaterTimeout`, `alarmExtractionTimeout`.
- **State / page classes** that hint at where each register group lives: `BoilerSettingState`, `BoilerParamsState`, `CleanParamsState`, `CompensationParamsState`, `DeviceMakingStatus`, `DeviceReadStatus`, `MachineConfig…`.

The Chinese strings in libapp.so are stored as **UTF-16 LE**, not UTF-8 — `strings` with default settings misses them. Use `python -c "open('libapp.so','rb').read().decode('utf-16-le', errors='replace')"` and grep on the result.

To go deeper than symbol mining (i.e. recover the register-address ↔ field-name mapping), the remaining option is [blutter](https://github.com/worawit/blutter), which disassembles Dart AOT snapshots. For now physical experimentation via `scripts/watch_state.py` is faster.

---

## 7. Open questions

- [x] BLE GATT service / TX char / RX char UUIDs ✓ § 1
- [x] BLE MTU ✓ § 1 (200 bytes negotiated on macOS); app-layer fragmentation for OTA 1024-byte packets still TBD
- [ ] Per-register meaning of the live-state block (regs 0–37) — most fields still unidentified; correlate by toggling physical controls and re-reading
- [ ] Confirmation that reg 14 (`0x02B7`) and reg 33 (`0x0258`) are temperatures, and which boiler each maps to
- [ ] Meaning of registers 154, 155, 1459
- [ ] Complete `FF55…` command catalog, including the structure of the spontaneous heartbeat (`FF 55 02 59 ...`) on `…2c10`
- [x] Whether the device requires any auth / pairing PIN ✓ § 1 (none — open central)
- [x] Response timing — does the device push state changes? ✓ § 6 (yes, ~2 Hz heartbeat on `…2c10`)
- [ ] How quick-key edits are committed (is there a "save" coil after writing a 167-reg block?)
- [ ] Whether the `FF55FFFF 9A 00 01 01 EF` event always fires on Modbus activity, or only on the first one per connection
- [x] Whether the steam wand can be actuated via BLE — **no, on the LITA-BA at least.** The official app has no steam-trigger button; the `steamdiancifa` symbol exists only in the service/diagnostic page `valves_widget.dart`. Steam wand is mechanical-only (turn knob). 2026-05-26.
- [ ] Whether the LITA-BR or other models in the family expose a BLE-actuated steam valve (possible — they have dedicated `machine_check_lita_hot_water_high/low.dart` variants)

---

## 8. Sources / tools

- Static analysis tooling: `unzip` + `jadx` + `strings`
- BLE client library used in our scripts: [bleak](https://github.com/hbldh/bleak) (works on macOS / Linux / Windows)
- Optional, for live capture: Android *Bluetooth HCI snoop log* → Wireshark
- Optional, for deeper Dart analysis if needed later: [blutter](https://github.com/worawit/blutter) (Flutter AOT snapshot disassembler)
