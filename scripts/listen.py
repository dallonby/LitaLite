"""Connect to a BLE device and subscribe to every notifiable characteristic.

Usage:
    .venv/bin/python scripts/listen.py <address-or-uuid> [--seconds N]

Prints every notification as it arrives, with a wall-clock timestamp and the
characteristic UUID. While this is running, drive the official Android app
(connect, change settings, pull a shot) — every state change the machine
broadcasts will land here. This is how we learn the response/event format
without needing the APK source.
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient


def hex_and_ascii(data: bytes) -> str:
    hexs = data.hex(" ")
    ascii_repr = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hexs}  |{ascii_repr}|"


async def main(address: str, seconds: float) -> None:
    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}\n")

        def make_handler(uuid: str):
            def handler(_sender, data: bytearray):
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"{ts}  {uuid}  {hex_and_ascii(bytes(data))}")
            return handler

        subscribed: list[str] = []
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    try:
                        await client.start_notify(char.uuid, make_handler(char.uuid))
                        subscribed.append(char.uuid)
                    except Exception as e:
                        print(f"# could not subscribe to {char.uuid}: {e}", file=sys.stderr)

        print(f"# subscribed to {len(subscribed)} characteristics; listening for {seconds}s")
        print(f"# drive the official app now\n")
        await asyncio.sleep(seconds)

        for uuid in subscribed:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--seconds", type=float, default=120.0)
    args = p.parse_args()
    asyncio.run(main(args.address, args.seconds))
