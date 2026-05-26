"""Live state watcher: poll one or more holding-register ranges in a loop and
watch the push-status channel, highlighting any register whose value changes.

Usage:
    # default: 3 ranges — the original "live state" guess, and two others the
    # official app polls (reg 387+16, reg 1404+20)
    .venv/bin/python scripts/watch_state.py <address>

    # custom ranges (comma-separated start:qty pairs)
    .venv/bin/python scripts/watch_state.py <address> --ranges 0:37,1404:20

    # single range
    .venv/bin/python scripts/watch_state.py <address> --ranges 1404:20

Default UUIDs are the ones discovered live on 2026-05-26:
    Modbus channel:        ...0c0d2b10
    Event/status channel:  ...0c0d2c10

While this is running, change something on the machine — turn the dial, change
the active profile, adjust a setpoint, run a brew. The columns that flip are
the registers carrying that field. That's how the live-state register map gets
filled in.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import build_read_coils, build_read_holding, parse_response  # noqa: E402

MODBUS_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b10"
EVENT_CHAR  = "00010203-0405-0607-0809-0a0b0c0d2c10"

# Three default ranges to survey simultaneously:
#   (0, 37):    the original "live state" candidate. Static analysis says this
#               is likely setpoints/standby, not telemetry — confirm by lack of
#               movement during steam actuation.
#   (387, 16):  PROTOCOL.md fixture `010301830010B412` reads 16 regs from 387.
#   (1404, 20): PROTOCOL.md describes 1404 as "Model-4 / status block,
#               frequently polled" — best candidate for live telemetry.
DEFAULT_RANGES = [(0, 37), (387, 16), (1404, 20)]


def parse_ranges(spec: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, q = part.split(":")
        out.append((int(a), int(q)))
    return out


async def _send_and_collect(client, cmd: bytes, modbus_buf: bytearray,
                            modbus_done: asyncio.Event,
                            timeout: float = 3.0) -> bytes | None:
    modbus_buf.clear()
    modbus_done.clear()
    try:
        await client.write_gatt_char(MODBUS_CHAR, cmd, response=False)
    except Exception as e:
        print(f"  [modbus] write failed: {e}")
        return None
    try:
        await asyncio.wait_for(modbus_done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  [modbus] timeout  buf={bytes(modbus_buf).hex(' ') or '(empty)'}")
        return None
    return bytes(modbus_buf)


async def poll_range(client, addr: int, qty: int, modbus_buf: bytearray,
                     modbus_done: asyncio.Event) -> list[int] | None:
    cmd = build_read_holding(addr=addr, qty=qty)
    raw = await _send_and_collect(client, cmd, modbus_buf, modbus_done)
    if raw is None:
        return None
    resp = parse_response(raw)
    if resp is None or resp.function != 0x03:
        print(f"  [modbus] unexpected response for addr={addr}: {raw.hex(' ')}")
        return None
    byte_count = resp.data[0]
    payload = resp.data[1:1 + byte_count]
    return [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]


async def poll_coils(client, addr: int, qty: int, modbus_buf: bytearray,
                     modbus_done: asyncio.Event) -> list[int] | None:
    cmd = build_read_coils(addr=addr, qty=qty)
    raw = await _send_and_collect(client, cmd, modbus_buf, modbus_done)
    if raw is None:
        return None
    resp = parse_response(raw)
    if resp is None or resp.function != 0x01:
        print(f"  [modbus] unexpected coil response for addr={addr}: {raw.hex(' ')}")
        return None
    byte_count = resp.data[0]
    payload = resp.data[1:1 + byte_count]
    # Coils are packed LSB-first: coil[addr+0] = bit 0 of payload[0], etc.
    bits: list[int] = []
    for i in range(qty):
        bits.append((payload[i // 8] >> (i % 8)) & 1)
    return bits


async def main(address: str, ranges: list[tuple[int, int]],
               coil_ranges: list[tuple[int, int]], interval: float) -> None:
    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}\n")

        modbus_buf = bytearray()
        modbus_done = asyncio.Event()

        def on_modbus(_sender, data: bytearray):
            modbus_buf.extend(data)
            if parse_response(bytes(modbus_buf)) is not None:
                modbus_done.set()

        last_event = {"frame": None, "count": 0}

        def on_event(_sender, data: bytearray):
            frame = bytes(data).hex(" ")
            if frame == last_event["frame"]:
                last_event["count"] += 1
            else:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                if last_event["frame"] is not None and last_event["count"] > 1:
                    print(f"  [event] (above repeated {last_event['count']} times)")
                print(f"  [event] {ts}  {frame}")
                last_event["frame"] = frame
                last_event["count"] = 1

        await client.start_notify(MODBUS_CHAR, on_modbus)
        await client.start_notify(EVENT_CHAR, on_event)

        print(f"Polling holding ranges {ranges}  +  coil ranges {coil_ranges}  every {interval}s. ^C to stop.\n")

        prev_by_range: dict[tuple[int, int], list[int] | None] = {r: None for r in ranges}
        prev_by_coil_range: dict[tuple[int, int], list[int] | None] = {r: None for r in coil_ranges}

        def print_header(addr: int, qty: int, cell_w: int = 5):
            cols = " ".join(f"{addr+i:>{cell_w-1}d}" for i in range(qty))
            print(f"   addr  {cols}")
            print("         " + "-" * (cell_w * qty))

        try:
            while True:
                for (addr, qty) in ranges:
                    values = await poll_range(client, addr, qty, modbus_buf, modbus_done)
                    if values is None:
                        continue
                    if prev_by_range[(addr, qty)] is None:
                        print_header(addr, qty, cell_w=5)
                    prev = prev_by_range[(addr, qty)]
                    cells = []
                    changed: list[int] = []
                    for i, v in enumerate(values):
                        if prev is not None and prev[i] != v:
                            cells.append(f"\033[1;33m{v:>4d}\033[0m")
                            changed.append(i)
                        else:
                            cells.append(f"{v:>4d}")
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"{ts} {addr:>5d}  " + " ".join(cells))
                    if changed and prev is not None:
                        diffs = ", ".join(
                            f"reg {addr+i}: {prev[i]} → {values[i]}" for i in changed
                        )
                        print(f"          \033[33m^^ changed: {diffs}\033[0m")
                    prev_by_range[(addr, qty)] = values

                for (addr, qty) in coil_ranges:
                    bits = await poll_coils(client, addr, qty, modbus_buf, modbus_done)
                    if bits is None:
                        continue
                    if prev_by_coil_range[(addr, qty)] is None:
                        print_header(addr, qty, cell_w=5)
                    prev = prev_by_coil_range[(addr, qty)]
                    cells = []
                    changed: list[int] = []
                    for i, v in enumerate(bits):
                        if prev is not None and prev[i] != v:
                            cells.append(f"\033[1;31m{v:>4d}\033[0m")  # red for coil changes
                            changed.append(i)
                        else:
                            cells.append(f"{v:>4d}")
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"{ts} c{addr:>4d}  " + " ".join(cells))
                    if changed and prev is not None:
                        diffs = ", ".join(
                            f"coil {addr+i}: {prev[i]} → {bits[i]}" for i in changed
                        )
                        print(f"          \033[31m^^ coil changed: {diffs}\033[0m")
                    prev_by_coil_range[(addr, qty)] = bits

                print()
                await asyncio.sleep(interval)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nStopping...")
        finally:
            try:
                await client.stop_notify(MODBUS_CHAR)
                await client.stop_notify(EVENT_CHAR)
            except Exception:
                pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--ranges", type=str, default=None,
                   help="Comma-separated start:qty pairs (e.g. '0:37,1404:20'). "
                        "Default: 0:37, 387:16, 1404:20.")
    p.add_argument("--coils", type=str, default="150:16",
                   help="Comma-separated coil start:qty pairs. Default 150:16 "
                        "(covers brew-start 150 + the suspected valve coils 154/155/157).")
    p.add_argument("--interval", type=float, default=2.0)
    args = p.parse_args()
    ranges = parse_ranges(args.ranges) if args.ranges else DEFAULT_RANGES
    coil_ranges = parse_ranges(args.coils) if args.coils else []
    try:
        asyncio.run(main(args.address, ranges, coil_ranges, args.interval))
    except KeyboardInterrupt:
        pass
