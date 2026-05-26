"""Determine which characteristic is TX (write) and which is RX (notify).

Usage:
    .venv/bin/python scripts/discover_endpoints.py <address>

Subscribes to every notifiable characteristic in the device's custom service,
then writes a safe Modbus read (`01 03 00 00 00 25 84 11` — read 37 holding
registers from address 0) to each writable characteristic in turn and reports
which char(s) respond.

This is a one-shot discovery step: once the TX/RX UUIDs are known, use
probe.py / a custom client directly.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import parse_response  # noqa: E402

SAFE_READ = bytes.fromhex("0103000000258411")  # read 37 holding regs from 0
STANDARD_SERVICES = {"00001800", "00001801"}  # GAP / GATT — skip


async def main(address: str) -> None:
    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}\n")

        notify_chars: list[str] = []
        write_chars: list[str] = []
        for service in client.services:
            if service.uuid[:8] in STANDARD_SERVICES:
                continue
            for char in service.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    notify_chars.append(char.uuid)
                if "write" in char.properties or "write-without-response" in char.properties:
                    write_chars.append(char.uuid)

        print(f"# notify candidates: {notify_chars}")
        print(f"# write candidates:  {write_chars}\n")

        # Track which char each notification came from.
        # Bleak passes a BleakGATTCharacteristic as `sender`; .uuid gives us the UUID.
        traffic: dict[str, list[bytes]] = {u: [] for u in notify_chars}

        def make_handler(uuid: str):
            def handler(_sender, data: bytearray):
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"  {ts}  notify <- {uuid[-8:]}  {bytes(data).hex(' ')}")
                traffic[uuid].append(bytes(data))
            return handler

        for u in notify_chars:
            await client.start_notify(u, make_handler(u))
        print(f"# subscribed to {len(notify_chars)} char(s)\n")

        for tx_uuid in write_chars:
            for u in traffic:
                traffic[u].clear()
            print(f"--- writing safe read to {tx_uuid[-8:]} ---")
            print(f"    tx: {SAFE_READ.hex(' ')}")
            try:
                await client.write_gatt_char(tx_uuid, SAFE_READ, response=False)
            except Exception as e:
                print(f"    write failed (write-without-response): {e}")
                try:
                    await client.write_gatt_char(tx_uuid, SAFE_READ, response=True)
                    print(f"    retried with write-with-response: OK")
                except Exception as e2:
                    print(f"    write failed (write-with-response): {e2}")
                    continue
            await asyncio.sleep(2.5)

            saw_any = False
            for u, frames in traffic.items():
                if not frames:
                    continue
                saw_any = True
                joined = b"".join(frames)
                resp = parse_response(joined)
                status = "CRC OK" if resp else "CRC fail / partial"
                print(f"    <- {u[-8:]}: {len(frames)} frame(s), {len(joined)} B  [{status}]")
                if resp:
                    print(f"       slave=0x{resp.slave:02X} fc=0x{resp.function:02X} "
                          f"data={resp.data.hex(' ')[:60]}...")
            if not saw_any:
                print(f"    (no notifications received in 2.5s)")
            print()

        for u in notify_chars:
            try:
                await client.stop_notify(u)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
