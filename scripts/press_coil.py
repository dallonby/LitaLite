"""Briefly press a Modbus coil ON, then OFF, while polling state and watching events.

Usage:
    .venv/bin/python scripts/press_coil.py <address> --coil 154 [--hold 2.0]

Sequence:
  1. Connect, subscribe to both notify channels.
  2. Baseline read of regs 1404-1423 and coils 150-165.
  3. Write coil <addr> ON.
  4. Poll regs+coils every 0.5s for --hold seconds.
  5. Write coil <addr> OFF.
  6. Final read; diff against baseline.

Watch the machine while this runs. If something fires (steam, water, click), note
what physically happened and we know which coil that is.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import (  # noqa: E402
    build_read_coils, build_read_holding, build_write_coil, parse_response,
)

MODBUS_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b10"
EVENT_CHAR  = "00010203-0405-0607-0809-0a0b0c0d2c10"


async def send_collect(client, cmd, buf, done, timeout=3.0):
    buf.clear()
    done.clear()
    await client.write_gatt_char(MODBUS_CHAR, cmd, response=False)
    await asyncio.wait_for(done.wait(), timeout=timeout)
    return bytes(buf)


async def read_regs(client, addr, qty, buf, done):
    raw = await send_collect(client, build_read_holding(addr=addr, qty=qty), buf, done)
    resp = parse_response(raw)
    if resp is None or resp.function != 0x03:
        return None
    bc = resp.data[0]
    p = resp.data[1:1 + bc]
    return [int.from_bytes(p[i:i+2], "big") for i in range(0, len(p), 2)]


async def read_coils(client, addr, qty, buf, done):
    raw = await send_collect(client, build_read_coils(addr=addr, qty=qty), buf, done)
    resp = parse_response(raw)
    if resp is None or resp.function != 0x01:
        return None
    bc = resp.data[0]
    p = resp.data[1:1 + bc]
    return [(p[i//8] >> (i % 8)) & 1 for i in range(qty)]


def fmt_diff(label, before, after, addr_start):
    if before is None or after is None:
        return f"  {label}: (read failure)"
    changes = []
    for i, (b, a) in enumerate(zip(before, after)):
        if b != a:
            changes.append(f"{addr_start+i}: {b} → {a}")
    if not changes:
        return f"  {label}: no changes"
    return f"  {label} changes: " + ", ".join(changes)


async def main(address, coil_addr, hold):
    buf = bytearray()
    done = asyncio.Event()
    events = []

    def on_modbus(_s, data: bytearray):
        buf.extend(data)
        if parse_response(bytes(buf)) is not None:
            done.set()

    def on_event(_s, data: bytearray):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        events.append((ts, bytes(data).hex(' ')))

    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}\n")
        await client.start_notify(MODBUS_CHAR, on_modbus)
        await client.start_notify(EVENT_CHAR, on_event)

        print("Reading baseline...")
        base_regs = await read_regs(client, 1404, 20, buf, done)
        base_coils = await read_coils(client, 150, 16, buf, done)
        print(f"  regs 1404+20:  {base_regs}")
        print(f"  coils 150-165: {base_coils}")
        events_before = len(events)

        print(f"\n>>> PRESSING coil {coil_addr} ON for {hold:.1f}s...")
        on_cmd = build_write_coil(addr=coil_addr, on=True)
        print(f"    cmd: {on_cmd.hex(' ')}")
        await client.write_gatt_char(MODBUS_CHAR, on_cmd, response=False)

        # During hold, sample state every 0.5s
        start = asyncio.get_event_loop().time()
        sample_no = 0
        while asyncio.get_event_loop().time() - start < hold:
            await asyncio.sleep(0.5)
            sample_no += 1
            try:
                regs = await read_regs(client, 1404, 20, buf, done)
                coils = await read_coils(client, 150, 16, buf, done)
                reg_diffs = [(1404+i, a) for i, (b, a) in enumerate(zip(base_regs, regs)) if b != a]
                coil_diffs = [(150+i, a) for i, (b, a) in enumerate(zip(base_coils, coils)) if b != a]
                if reg_diffs or coil_diffs:
                    print(f"  +{sample_no*0.5:.1f}s  regs: {reg_diffs}  coils: {coil_diffs}")
                else:
                    print(f"  +{sample_no*0.5:.1f}s  (no diff)")
            except Exception as e:
                print(f"  +{sample_no*0.5:.1f}s  poll failed: {e}")

        print(f"\n>>> RELEASING coil {coil_addr} (writing OFF)")
        off_cmd = build_write_coil(addr=coil_addr, on=False)
        print(f"    cmd: {off_cmd.hex(' ')}")
        await client.write_gatt_char(MODBUS_CHAR, off_cmd, response=False)
        await asyncio.sleep(0.5)

        print("\nFinal state vs baseline:")
        final_regs = await read_regs(client, 1404, 20, buf, done)
        final_coils = await read_coils(client, 150, 16, buf, done)
        print(fmt_diff("regs 1404+20", base_regs, final_regs, 1404))
        print(fmt_diff("coils 150+16", base_coils, final_coils, 150))

        if len(events) > events_before:
            print(f"\nEvents on …2c10 during press ({len(events) - events_before} new):")
            for ts, h in events[events_before:]:
                print(f"  {ts}  {h}")

        try:
            await client.stop_notify(MODBUS_CHAR)
            await client.stop_notify(EVENT_CHAR)
        except Exception:
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--coil", type=int, required=True, help="Coil address to press (e.g. 154)")
    p.add_argument("--hold", type=float, default=2.0, help="Seconds to hold ON before releasing")
    args = p.parse_args()
    try:
        asyncio.run(main(args.address, args.coil, args.hold))
    except KeyboardInterrupt:
        pass
