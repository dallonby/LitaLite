"""Trigger a brew and record telemetry to CSV for later graphing / replay.

Polls the live-telemetry block (regs 1404-1423) at ~5 Hz over Modbus,
subscribes to the FF55 event push, and captures everything to a CSV with
millisecond timestamps. Stops automatically when the shot finishes (pressure
falls to 0 for several consecutive samples).

Usage:
    # press coil 150 to start, record until shot ends, save to file
    .venv/bin/python scripts/record_brew.py <address> --start --out brew.csv

    # do nothing — just sit listening, you press M / the app fires the brew
    .venv/bin/python scripts/record_brew.py <address> --out brew.csv

    # also push a profile first
    .venv/bin/python scripts/record_brew.py <address> --send-demo --start --out brew.csv

CSV columns:
    t_ms,              elapsed ms since recording started
    elapsed_brew_ms,   ms since pressure first rose above threshold (None until then)
    pressure_bar,      reg 1410 / 10
    flow_ml_s,         reg 1422 / 10
    volume_ml,         reg 1411
    shot_time_s,       reg 1417
    progress_pct,      reg 1405
    brew_temp_c,       reg 1409 / 10
    steam_temp_c,      reg 1408 / 10
    raw_regs,          full reg 1404..1423 as comma-joined ints
    event_hex,         FF55 event frame if any arrived this sample, else empty
"""

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bleak import BleakClient  # noqa: E402

from modbus import (  # noqa: E402
    build_read_holding, build_write_coil, parse_response,
)
from send_profile import (  # noqa: E402
    KEY_BASE, MODBUS_CHAR, EVENT_CHAR, START_BREW_COIL,
    compile_writes, make_demo_profile,
)

LIVE_REG_BASE = 1404
LIVE_REG_QTY = 20
POLL_INTERVAL_S = 0.2
PRESSURE_RISE_THRESHOLD = 5      # reg 1410 raw (= 0.5 bar)
# Shot is "over" when BOTH pressure has been 0 AND volume hasn't changed for
# this many seconds. The volume check survives multi-stage `wait_time` gaps
# (during which pressure drops to 0 but the shot isn't actually done).
IDLE_SECONDS_TO_END = 12.0


async def send_one(client, cmd, buf, done, timeout=2.0):
    buf.clear(); done.clear()
    await client.write_gatt_char(MODBUS_CHAR, cmd, response=False)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    return bytes(buf)


def decode_live_regs(resp_bytes: bytes) -> list[int] | None:
    rp = parse_response(resp_bytes)
    if rp is None or rp.function != 0x03:
        return None
    bc = rp.data[0]
    p = rp.data[1:1 + bc]
    return [int.from_bytes(p[i:i+2], "big") for i in range(0, len(p), 2)]


async def main(address: str, out_path: Path, do_start: bool,
               do_send_demo: bool, send_key: int, max_duration_s: float) -> None:
    buf = bytearray()
    done = asyncio.Event()
    pending_events: list[str] = []

    def on_modbus(_s, data: bytearray):
        buf.extend(data)
        if parse_response(bytes(buf)) is not None:
            done.set()

    def on_event(_s, data: bytearray):
        pending_events.append(bytes(data).hex(' '))

    print(f"Connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"Connected. MTU: {client.mtu_size}")
        await client.start_notify(MODBUS_CHAR, on_modbus)
        await client.start_notify(EVENT_CHAR, on_event)

        if do_send_demo:
            print("Sending TestyT demo profile...")
            for desc, raw in compile_writes(make_demo_profile(), send_key):
                resp = await send_one(client, raw, buf, done)
                print(f"  {'ok' if resp else '??'}  {desc}")

        if do_start:
            print(f"Pressing coil {START_BREW_COIL} to start brew...")
            await send_one(client, build_write_coil(addr=START_BREW_COIL, on=True), buf, done)
            await asyncio.sleep(0.1)
            await send_one(client, build_write_coil(addr=START_BREW_COIL, on=False), buf, done)
        else:
            print(f"Idle recording — start the brew manually (M button or app).")

        out = out_path.open("w", newline="")
        writer = csv.writer(out)
        writer.writerow([
            "t_ms", "elapsed_brew_ms",
            "pressure_bar", "flow_ml_s", "volume_ml",
            "shot_time_s", "progress_pct",
            "brew_temp_c", "steam_temp_c",
            "raw_regs", "event_hex",
        ])

        t0 = time.monotonic()
        brew_start_t: float | None = None
        last_volume = 0
        last_volume_change_t = t0
        read_cmd = build_read_holding(addr=LIVE_REG_BASE, qty=LIVE_REG_QTY)

        print(f"\nRecording to {out_path} (poll every {POLL_INTERVAL_S*1000:.0f}ms). ^C to stop.\n")

        try:
            while True:
                resp = await send_one(client, read_cmd, buf, done, timeout=2.0)
                regs = decode_live_regs(resp) if resp else None
                now = time.monotonic()
                t_ms = int((now - t0) * 1000)

                # Drain any FF55 events that arrived since last poll
                event_hex = ""
                if pending_events:
                    event_hex = " | ".join(pending_events)
                    pending_events.clear()

                if regs is None:
                    print(f"  {t_ms:7d}ms  read failed")
                else:
                    # offset from LIVE_REG_BASE
                    pressure_raw = regs[1410 - LIVE_REG_BASE]
                    flow_raw     = regs[1422 - LIVE_REG_BASE]
                    volume       = regs[1411 - LIVE_REG_BASE]
                    shot_time    = regs[1417 - LIVE_REG_BASE]
                    progress     = regs[1405 - LIVE_REG_BASE]
                    brew_temp    = regs[1409 - LIVE_REG_BASE]
                    steam_temp   = regs[1408 - LIVE_REG_BASE]

                    # Detect brew start: pressure crosses up
                    if brew_start_t is None and pressure_raw >= PRESSURE_RISE_THRESHOLD:
                        brew_start_t = now
                        print(f"  {t_ms:7d}ms  → brew started")
                    elapsed_brew_ms = None
                    if brew_start_t is not None:
                        elapsed_brew_ms = int((now - brew_start_t) * 1000)

                    writer.writerow([
                        t_ms,
                        elapsed_brew_ms if elapsed_brew_ms is not None else "",
                        pressure_raw / 10.0,
                        flow_raw / 10.0,
                        volume,
                        shot_time,
                        progress,
                        brew_temp / 10.0,
                        steam_temp / 10.0,
                        ",".join(str(v) for v in regs),
                        event_hex,
                    ])
                    out.flush()

                    # Per-sample console line
                    marker = ""
                    if brew_start_t is None:
                        marker = "(idle)"
                    elif pressure_raw == 0:
                        marker = "(p=0)"
                    print(f"  {t_ms:7d}ms  p={pressure_raw/10:>4.1f}bar  "
                          f"f={flow_raw/10:>4.1f}mL/s  vol={volume:>3}mL  "
                          f"t={shot_time:>3}s  prog={progress:>3}%  "
                          f"brewT={brew_temp/10:>5.1f}  steamT={steam_temp/10:>5.1f}  "
                          f"{marker}")

                    # End detection: after brew has started, declare it done when
                    # BOTH pressure is 0 AND volume has not changed for
                    # IDLE_SECONDS_TO_END. Volume-stable means we're not in a
                    # mid-shot wait_time gap (where pressure drops but the
                    # machine is still mid-recipe).
                    if brew_start_t is not None:
                        if volume != last_volume:
                            last_volume = volume
                            last_volume_change_t = now
                        idle_for = now - last_volume_change_t
                        if pressure_raw == 0 and idle_for >= IDLE_SECONDS_TO_END:
                            print(f"  {t_ms:7d}ms  → shot complete "
                                  f"(p=0 + volume stable for {idle_for:.1f}s)")
                            break

                    # Safety cap
                    if max_duration_s and (now - t0) > max_duration_s:
                        print(f"  hit --max-duration {max_duration_s}s, stopping")
                        break

                # Sleep for the rest of the interval (poll takes some time)
                elapsed = time.monotonic() - now
                sleep_for = max(0, POLL_INTERVAL_S - elapsed)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n^C")
        finally:
            out.close()
            try:
                await client.stop_notify(MODBUS_CHAR)
                await client.stop_notify(EVENT_CHAR)
            except Exception:
                pass

        print(f"\nSaved CSV: {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--out", type=Path, default=Path("captures") / f"brew_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    p.add_argument("--start", action="store_true",
                   help="Press coil 150 to start brew (otherwise just sit and listen)")
    p.add_argument("--send-demo", action="store_true",
                   help="Send the TestyT demo profile before starting")
    p.add_argument("--key", type=int, default=1, choices=list(KEY_BASE.keys()),
                   help="Which key slot to send the demo profile into (1-4)")
    p.add_argument("--max-duration", type=float, default=120.0,
                   help="Safety cap on recording duration in seconds")
    args = p.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(main(args.address, args.out, args.start, args.send_demo,
                         args.key, args.max_duration))
    except KeyboardInterrupt:
        pass
