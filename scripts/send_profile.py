"""Send a brew profile to the Wendougee espresso machine via BLE.

Reproduces the exact write sequence the official Android app emits when you
tap a quick-key to brew. Captured + verified live by HCI snoop on 2026-05-26
(see PROTOCOL.md § 3 "Full live-confirmed sequence").

Usage:
    .venv/bin/python scripts/send_profile.py <address> [--key 1] [--brew]
        [--profile PATH.json | --demo]

Profile JSON format (matches the in-app data model):

    {
        "real_mode": 2,            # 0..4 per craft.real_mode taxonomy
        "callswitch": "flow",      # "flow" or "weight"
        "direct": false,           # true = single-stage direct extract
        "changeswitch": true,      # true = variable flow allowed
        "total_flow": 68,          # mL  (set if flow mode)
        "total_weight": 0,         # g   (set if weight mode)
        "auto_link": 0,
        "stages": [
            {"time": 7,  "pressure_bar": 6.1, "priority": "pressure", "wait_time": 8},
            {"time": 3,  "pressure_bar": 2.3, "priority": "pressure", "wait_time": 0},
            {"time": 20, "flow_ml_s":   1.7, "priority": "flow",     "wait_time": 1},
            {"time": 4,  "pressure_bar": 1.1, "priority": "pressure", "wait_time": 0}
        ]
    }

Use --demo to send the exact captured TestyT profile (regression test).
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import (  # noqa: E402
    build_read_holding, build_write_coil, build_write_register,
    build_write_registers, parse_response,
)

MODBUS_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b10"
EVENT_CHAR  = "00010203-0405-0607-0809-0a0b0c0d2c10"
KEY_BASE = {1: 2048, 2: 2560, 3: 3048, 4: 3560}
ACTIVE_MODE_REG = 87
START_BREW_COIL = 150


@dataclass
class Stage:
    time: int                          # seconds
    pressure_bar: float = 0.0          # bar (will be ×10 on the wire)
    flow_ml_s: float = 0.0             # mL/s (will be ×10 on the wire)
    wait_time: int = 0                 # seconds, pause after this stage
    priority: str = "pressure"         # "pressure" or "flow"


@dataclass
class Profile:
    real_mode: int                     # 0..4 per craft.real_mode taxonomy
    callswitch: str = "flow"           # "flow" or "weight"
    direct: bool = False               # single-stage
    changeswitch: bool = True          # variable flow allowed
    total_flow: int = 0                # mL
    total_weight: int = 0              # g
    auto_link: int = 0
    stages: list[Stage] = field(default_factory=list)


def header_values(p: Profile) -> list[int]:
    """Build the 7-reg header. JS inverts callswitch & direct before sending."""
    return [
        0 if p.callswitch == "weight" else 1,   # callswitch (inverted)
        1 if p.real_mode in (2, 3) else 0,      # mode: 1 = variable pressure
        0 if p.direct else 1,                   # direct (inverted)
        1 if p.changeswitch else 0,             # changeswitch
        p.total_flow,
        p.total_weight,
        p.auto_link,
    ]


def stage_values(s: Stage, is_last: bool) -> list[int]:
    """Build a 6-reg stage block."""
    priority_bit = 1 if s.priority == "flow" else 0
    pressure_x10 = 0 if priority_bit == 1 else round(s.pressure_bar * 10)
    flow_x10     = round(s.flow_ml_s * 10) if priority_bit == 1 else 0
    return [
        int(s.time),
        int(pressure_x10),
        int(flow_x10),
        0 if is_last else int(s.wait_time),
        1 if is_last else 0,
        priority_bit,
    ]


def compile_writes(p: Profile, key: int) -> list[tuple[str, bytes]]:
    """Return the ordered list of (description, raw_modbus_bytes) writes."""
    if key not in KEY_BASE:
        raise ValueError(f"key must be 1..4, got {key}")
    base = KEY_BASE[key]
    writes: list[tuple[str, bytes]] = []

    # 1. Header (7 regs)
    writes.append(
        (f"header @{base}",
         build_write_registers(addr=base, values=header_values(p)))
    )

    # 2. Stages (6 regs each, 9-reg stride starting at base+8)
    if p.direct:
        if not p.stages:
            raise ValueError("direct mode still needs one stage providing flow_ml_s")
        s = p.stages[0]
        flow_x10 = round(s.flow_ml_s * 10) if s.flow_ml_s else round((p.total_flow or 0) * 10 / max(1, s.time))
        writes.append(
            (f"direct stage @{base + 8}",
             build_write_registers(addr=base + 8, values=[0, 0, flow_x10, 0, 1]))
        )
    else:
        addr = base + 8
        for i, s in enumerate(p.stages):
            is_last = (i == len(p.stages) - 1)
            writes.append(
                (f"stage {i+1} @{addr}",
                 build_write_registers(addr=addr, values=stage_values(s, is_last)))
            )
            addr += 9   # 6 regs + 3-reg gap

    # 3. Active mode selector
    writes.append(
        (f"active mode reg {ACTIVE_MODE_REG} ← {p.real_mode}",
         build_write_register(addr=ACTIVE_MODE_REG, value=p.real_mode))
    )

    return writes


def make_demo_profile() -> Profile:
    """The TestyT profile captured live on 2026-05-26."""
    return Profile(
        real_mode=2,                # Flow variable pressure
        callswitch="flow",
        direct=False,
        changeswitch=True,
        total_flow=68,
        total_weight=0,
        auto_link=0,
        stages=[
            Stage(time=7,  pressure_bar=6.1, priority="pressure", wait_time=8),
            Stage(time=3,  pressure_bar=2.3, priority="pressure", wait_time=0),
            Stage(time=20, flow_ml_s=1.7,    priority="flow",     wait_time=1),
            Stage(time=4,  pressure_bar=1.1, priority="pressure", wait_time=0),
        ],
    )


def load_profile(path: Path) -> Profile:
    raw = json.loads(path.read_text())
    return Profile(
        real_mode=raw["real_mode"],
        callswitch=raw.get("callswitch", "flow"),
        direct=raw.get("direct", False),
        changeswitch=raw.get("changeswitch", True),
        total_flow=raw.get("total_flow", 0),
        total_weight=raw.get("total_weight", 0),
        auto_link=raw.get("auto_link", 0),
        stages=[
            Stage(
                time=s["time"],
                pressure_bar=s.get("pressure_bar", 0),
                flow_ml_s=s.get("flow_ml_s", 0),
                wait_time=s.get("wait_time", 0),
                priority=s.get("priority", "pressure"),
            )
            for s in raw["stages"]
        ],
    )


async def send(client: BleakClient, cmd: bytes,
               buf: bytearray, done: asyncio.Event,
               timeout: float = 2.0) -> bytes | None:
    buf.clear(); done.clear()
    await client.write_gatt_char(MODBUS_CHAR, cmd, response=False)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    return bytes(buf)


async def main(address: str, key: int, profile: Profile, brew: bool, verify: bool) -> None:
    writes = compile_writes(profile, key)

    print("Compiled write sequence:")
    for desc, raw in writes:
        print(f"  {desc:35s}  {raw.hex(' ')}")
    if brew:
        print(f"  start brew coil {START_BREW_COIL} ON  "
              f"{build_write_coil(addr=START_BREW_COIL, on=True).hex(' ')}")
        print(f"  start brew coil {START_BREW_COIL} OFF "
              f"{build_write_coil(addr=START_BREW_COIL, on=False).hex(' ')}")

    print(f"\nConnecting to {address}...")
    buf = bytearray()
    done = asyncio.Event()

    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}")

        def on_modbus(_s, data: bytearray):
            buf.extend(data)
            if parse_response(bytes(buf)) is not None:
                done.set()

        await client.start_notify(MODBUS_CHAR, on_modbus)

        for desc, raw in writes:
            resp = await send(client, raw, buf, done)
            if resp is None:
                print(f"  !! {desc}: no/invalid response")
            else:
                rp = parse_response(resp)
                ok = "ok" if rp and rp.function in (0x10, 0x06) else "?"
                print(f"  {ok}  {desc}")

        if brew:
            print(f"\nStarting brew (coil {START_BREW_COIL})...")
            await send(client, build_write_coil(addr=START_BREW_COIL, on=True), buf, done)
            await asyncio.sleep(0.1)
            await send(client, build_write_coil(addr=START_BREW_COIL, on=False), buf, done)

        if verify:
            # Read back the header to confirm.
            base = KEY_BASE[key]
            resp = await send(client, build_read_holding(addr=base, qty=7), buf, done)
            rp = parse_response(resp) if resp else None
            if rp and rp.function == 0x03:
                bc = rp.data[0]
                regs = [int.from_bytes(rp.data[1+2*i:3+2*i], "big") for i in range(bc//2)]
                expected = header_values(profile)
                match = "✓" if regs == expected else "✗"
                print(f"\nVerify read @{base}: {regs}  expected: {expected}  {match}")

        try:
            await client.stop_notify(MODBUS_CHAR)
        except Exception:
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--key", type=int, default=1, choices=[1, 2, 3, 4])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--profile", type=Path)
    src.add_argument("--demo", action="store_true",
                     help="Send the TestyT demo profile (regression test)")
    p.add_argument("--brew", action="store_true",
                   help="Also press coil 150 to start the shot")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip read-back verification")
    args = p.parse_args()
    profile = make_demo_profile() if args.demo else load_profile(args.profile)
    asyncio.run(main(args.address, args.key, profile, args.brew, not args.no_verify))
