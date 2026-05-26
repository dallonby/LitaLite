"""Briefly press a Modbus holding register (write 1, hold, write 0) while polling state.

Mirror of press_coil.py but using function code 0x06 on a holding register, for
controls that the app actuates as a register write rather than a coil write.

Usage:
    .venv/bin/python scripts/press_register.py <address> --reg 1459 [--hold 2.0]
        [--on-value 1] [--off-value 0]
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import (  # noqa: E402
    build_read_coils, build_read_holding, build_write_register, parse_response,
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


def diff(label, before, after, addr_start):
    if before is None or after is None:
        return f"  {label}: (read failure)"
    changes = [(addr_start+i, b, a) for i, (b, a) in enumerate(zip(before, after)) if b != a]
    if not changes:
        return f"  {label}: no changes"
    return f"  {label}: " + ", ".join(f"{a}: {bv} → {av}" for a, bv, av in changes)


async def main(address, reg_addr, on_val, off_val, hold):
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
        base_target = await read_regs(client, reg_addr, 1, buf, done)
        print(f"  regs 1404+20:   {base_regs}")
        print(f"  coils 150-165:  {base_coils}")
        print(f"  reg {reg_addr} initial: {base_target}")
        events_before = len(events)

        on_cmd = build_write_register(addr=reg_addr, value=on_val)
        off_cmd = build_write_register(addr=reg_addr, value=off_val)
        print(f"\n>>> WRITING reg {reg_addr} ← {on_val}  (cmd: {on_cmd.hex(' ')})")
        await client.write_gatt_char(MODBUS_CHAR, on_cmd, response=False)

        start = asyncio.get_event_loop().time()
        sample = 0
        while asyncio.get_event_loop().time() - start < hold:
            await asyncio.sleep(0.5)
            sample += 1
            try:
                regs = await read_regs(client, 1404, 20, buf, done)
                coils = await read_coils(client, 150, 16, buf, done)
                cur = await read_regs(client, reg_addr, 1, buf, done)
                reg_diffs = [(1404+i, a) for i, (b, a) in enumerate(zip(base_regs, regs)) if b != a]
                coil_diffs = [(150+i, a) for i, (b, a) in enumerate(zip(base_coils, coils)) if b != a]
                print(f"  +{sample*0.5:.1f}s  reg {reg_addr}={cur}  regs1404+: {reg_diffs}  coils: {coil_diffs}")
            except Exception as e:
                print(f"  +{sample*0.5:.1f}s  poll failed: {e}")

        print(f"\n>>> WRITING reg {reg_addr} ← {off_val}  (cmd: {off_cmd.hex(' ')})")
        await client.write_gatt_char(MODBUS_CHAR, off_cmd, response=False)
        await asyncio.sleep(0.5)

        print("\nFinal state vs baseline:")
        final_regs = await read_regs(client, 1404, 20, buf, done)
        final_coils = await read_coils(client, 150, 16, buf, done)
        final_target = await read_regs(client, reg_addr, 1, buf, done)
        print(diff("regs 1404+20",  base_regs,   final_regs,  1404))
        print(diff("coils 150+16",  base_coils,  final_coils, 150))
        print(f"  reg {reg_addr} final: {final_target}")

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
    p.add_argument("--reg", type=int, required=True, help="Holding-register address to press")
    p.add_argument("--on-value",  type=int, default=1)
    p.add_argument("--off-value", type=int, default=0)
    p.add_argument("--hold", type=float, default=2.0)
    args = p.parse_args()
    try:
        asyncio.run(main(args.address, args.reg, args.on_value, args.off_value, args.hold))
    except KeyboardInterrupt:
        pass
