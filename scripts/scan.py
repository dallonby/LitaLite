"""Scan for nearby BLE devices.

Usage:
    .venv/bin/python scripts/scan.py [--seconds N] [--filter SUBSTRING]

Look for a device whose name contains 'wendougee', 'espresso', or whatever the
machine advertises. Note the address (or UUID on macOS) for use with dump_gatt.py.
"""

import argparse
import asyncio

from bleak import BleakScanner


async def main(seconds: float, name_filter: str | None) -> None:
    print(f"Scanning for {seconds}s...")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)

    rows: list[tuple[int, str, str, str]] = []
    for addr, (device, adv) in devices.items():
        name = device.name or adv.local_name or "(unnamed)"
        if name_filter and name_filter.lower() not in name.lower():
            continue
        rssi = adv.rssi if adv.rssi is not None else -999
        services = ",".join(adv.service_uuids) if adv.service_uuids else ""
        rows.append((rssi, addr, name, services))

    rows.sort(key=lambda r: r[0], reverse=True)

    print(f"\n{'RSSI':>5}  {'ADDRESS':<40}  {'NAME':<30}  SERVICES")
    print("-" * 120)
    for rssi, addr, name, services in rows:
        print(f"{rssi:>5}  {addr:<40}  {name:<30}  {services}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--filter", dest="name_filter", default=None,
                   help="Only show devices whose name contains this substring")
    args = p.parse_args()
    asyncio.run(main(args.seconds, args.name_filter))
