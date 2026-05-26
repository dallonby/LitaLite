"""Send a single Modbus frame to the device and accumulate the notify response.

Usage:
    .venv/bin/python scripts/probe.py <address> \
        --tx <write-char-uuid> --rx <notify-char-uuid> \
        --hex 0103000000258411

The Wendougee device fragments responses across BLE notifications. We accumulate
incoming bytes until parse_response() succeeds (CRC validates) or we time out.

Start with a read packet (function code 0x01 or 0x03) — writes change device state.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import parse_response  # noqa: E402


async def main(address: str, tx_uuid: str, rx_uuid: str, hex_frame: str,
               timeout: float, write_with_response: bool) -> None:
    frame = bytes.fromhex(hex_frame)
    buf = bytearray()
    done = asyncio.Event()

    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}")

        def on_notify(_sender, data: bytearray):
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"  {ts}  notify  {bytes(data).hex(' ')}")
            buf.extend(data)
            resp = parse_response(bytes(buf))
            if resp is not None:
                done.set()

        await client.start_notify(rx_uuid, on_notify)
        print(f"Subscribed to {rx_uuid}")

        print(f"Writing to {tx_uuid}: {frame.hex(' ')}")
        await client.write_gatt_char(tx_uuid, frame, response=write_with_response)

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"\n# timeout after {timeout}s. Accumulated buffer:")
            print(f"  {bytes(buf).hex(' ') if buf else '(nothing)'}")
            print("# CRC did not validate. Possible reasons:")
            print("#   - need write-with-response (re-run with --with-response)")
            print("#   - wrong RX characteristic")
            print("#   - device requires a different framing / auth handshake first")
            return

        resp = parse_response(bytes(buf))
        assert resp is not None
        print()
        print(f"OK  slave=0x{resp.slave:02X}  fc=0x{resp.function:02X}  "
              f"data ({len(resp.data)} B): {resp.data.hex(' ')}")
        if resp.function == 0x03 and len(resp.data) >= 1:
            byte_count = resp.data[0]
            regs = resp.data[1:1 + byte_count]
            values = [int.from_bytes(regs[i:i + 2], "big") for i in range(0, len(regs), 2)]
            print(f"     decoded {len(values)} register(s): {values}")

        await client.stop_notify(rx_uuid)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address", help="BLE address / CoreBluetooth UUID from scan.py")
    p.add_argument("--tx", required=True, help="Write characteristic UUID")
    p.add_argument("--rx", required=True, help="Notify characteristic UUID")
    p.add_argument("--hex", required=True, help="Frame hex bytes to send (no spaces)")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--with-response", action="store_true",
                   help="Use write-with-response (default: write-without-response)")
    args = p.parse_args()
    asyncio.run(main(args.address, args.tx, args.rx, args.hex,
                     args.timeout, args.with_response))
