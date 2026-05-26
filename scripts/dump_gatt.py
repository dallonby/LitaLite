"""Connect to a BLE device and dump its full GATT structure.

Usage:
    .venv/bin/python scripts/dump_gatt.py <address-or-uuid>

For every service, characteristic, and descriptor, print the UUID, properties,
and (where readable) the current value as hex + decoded ASCII. This is the
authoritative map of what the device exposes — every command we'll later send
goes to one of these characteristics.

Save the output as captures/gatt_<timestamp>.txt for reference.
"""

import asyncio
import sys
from datetime import datetime

from bleak import BleakClient


def hex_and_ascii(data: bytes) -> str:
    hexs = data.hex(" ")
    ascii_repr = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hexs}  |{ascii_repr}|"


async def main(address: str) -> None:
    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}\n")

        for service in client.services:
            print(f"[Service] {service.uuid}  {service.description}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                line = f"  [Char] {char.uuid}  ({props})  {char.description}"
                value_str = ""
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        value_str = f"\n        value: {hex_and_ascii(value)}"
                    except Exception as e:
                        value_str = f"\n        read failed: {e}"
                print(line + value_str)
                for desc in char.descriptors:
                    try:
                        dval = await client.read_gatt_descriptor(desc.handle)
                        print(f"      [Desc] {desc.uuid}  {hex_and_ascii(dval)}")
                    except Exception as e:
                        print(f"      [Desc] {desc.uuid}  (read failed: {e})")
            print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    target = sys.argv[1]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"# GATT dump for {target} at {ts}\n")
    asyncio.run(main(target))
