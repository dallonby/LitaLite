"""Parse an Android btsnoop_hci.log and extract the ATT traffic of interest.

Usage:
    .venv/bin/python scripts/parse_snoop.py captures/snoop/btsnoop_hci.log [--mac AA:BB...]

Filters out everything except ATT writes (opcode 0x52, 0x12) and notifications
(0x1B) and prints them with a wall-clock timestamp. If a Wendougee Modbus frame
is recognised (slave=01, valid CRC16), it's annotated with the decoded function
code and parameters.

ACL fragmentation handling: minimal — Modbus packets fit in single ACL packets
on this device's MTU of ~200B, so we don't reassemble. A WARN is printed for
any L2CAP packet that looks fragmented (so we'd know).
"""

import argparse
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from modbus import crc16_modbus  # noqa: E402


# btsnoop epoch: Jan 1, 0 AD; 0x00DCDDB30F2F8000 microseconds = Jan 1 1970.
BTSNOOP_EPOCH_DELTA = 0x00DCDDB30F2F8000


def fmt_ts(ts_us: int) -> str:
    unix_us = ts_us - BTSNOOP_EPOCH_DELTA
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=unix_us)
    return dt.astimezone().strftime("%H:%M:%S.%f")[:-3]


def try_parse_modbus(data: bytes) -> str | None:
    """If data looks like a valid Modbus RTU packet (slave=01, CRC valid), decode it."""
    if len(data) < 4 or data[0] != 0x01:
        return None
    body, crc_bytes = data[:-2], data[-2:]
    expected = crc_bytes[0] | (crc_bytes[1] << 8)
    if crc16_modbus(body) != expected:
        return None
    fc = data[1]
    if fc == 0x03 and len(data) >= 8 and len(data) - 6 != data[2]:
        # request side: addr+qty
        addr = (data[2] << 8) | data[3]
        qty = (data[4] << 8) | data[5]
        return f"Modbus RD-HOLD addr={addr} qty={qty}"
    if fc == 0x03:
        # response: byte_count + data
        bc = data[2]
        regs = [(data[3 + 2*i] << 8) | data[4 + 2*i] for i in range(bc // 2)]
        return f"Modbus RD-HOLD-RESP bc={bc} regs={regs}"
    if fc == 0x01 and len(data) == 8:
        addr = (data[2] << 8) | data[3]
        qty = (data[4] << 8) | data[5]
        return f"Modbus RD-COIL addr={addr} qty={qty}"
    if fc == 0x01:
        bc = data[2]
        bits = []
        for i in range(bc * 8):
            bits.append((data[3 + i // 8] >> (i % 8)) & 1)
        return f"Modbus RD-COIL-RESP bc={bc} bits={bits}"
    if fc == 0x05 and len(data) == 8:
        addr = (data[2] << 8) | data[3]
        val = "ON " if data[4:6] == b"\xff\x00" else "OFF"
        return f"Modbus WR-COIL addr={addr} {val}"
    if fc == 0x06 and len(data) == 8:
        addr = (data[2] << 8) | data[3]
        val = (data[4] << 8) | data[5]
        return f"Modbus WR-REG addr={addr} value={val}"
    if fc == 0x10 and len(data) == 8:
        # response to write multiple: 01 10 addr_hi addr_lo qty_hi qty_lo crc_lo crc_hi
        addr = (data[2] << 8) | data[3]
        qty = (data[4] << 8) | data[5]
        return f"Modbus WR-REGS-RESP addr={addr} qty={qty}"
    if fc == 0x10 and len(data) >= 9:
        # request: 01 10 addr_hi addr_lo qty_hi qty_lo bytecount data... crc
        addr = (data[2] << 8) | data[3]
        qty = (data[4] << 8) | data[5]
        bc = data[6]
        if len(data) >= 7 + bc + 2 and qty * 2 == bc:
            regs = [(data[7 + 2*i] << 8) | data[8 + 2*i] for i in range(qty)]
            return f"Modbus WR-REGS addr={addr} qty={qty} values={regs}"
        return f"Modbus WR-REGS addr={addr} qty={qty} (truncated)"
    return f"Modbus fc=0x{fc:02X} data={data[2:-2].hex(' ')}"


def try_parse_ff55(data: bytes) -> str | None:
    if len(data) >= 4 and data[0] == 0xFF and data[1] == 0x55:
        return f"FF55 frame: {data.hex(' ')}"
    return None


ATT_NAMES = {
    0x01: "ERROR_RESP", 0x02: "MTU_REQ", 0x03: "MTU_RESP",
    0x04: "FIND_INFO_REQ", 0x05: "FIND_INFO_RESP",
    0x06: "FIND_BY_TYPE_REQ", 0x07: "FIND_BY_TYPE_RESP",
    0x08: "READ_BY_TYPE_REQ", 0x09: "READ_BY_TYPE_RESP",
    0x0A: "READ_REQ", 0x0B: "READ_RESP",
    0x10: "READ_BY_GROUP_REQ", 0x11: "READ_BY_GROUP_RESP",
    0x12: "WRITE_REQ", 0x13: "WRITE_RESP",
    0x52: "WRITE_CMD",
    0x16: "PREPARE_WRITE_REQ", 0x17: "PREPARE_WRITE_RESP",
    0x18: "EXEC_WRITE_REQ",  0x19: "EXEC_WRITE_RESP",
    0x1B: "NOTIFY", 0x1D: "INDICATE", 0x1E: "INDICATE_CONFIRM",
}


def parse(path: Path, mac_filter: str | None) -> None:
    data = path.read_bytes()
    if data[:8] != b"btsnoop\0":
        print("Not a btsnoop file", file=sys.stderr)
        sys.exit(1)
    version = struct.unpack(">I", data[8:12])[0]
    datalink = struct.unpack(">I", data[12:16])[0]
    print(f"# btsnoop v{version} datalink={datalink}", file=sys.stderr)

    off = 16
    rec_no = 0
    # Map (acl_handle) → MAC, learned from connection complete events
    handle_to_addr: dict[int, str] = {}

    while off < len(data):
        if off + 24 > len(data):
            break
        orig_len, incl_len, flags, _drops = struct.unpack(">IIII", data[off:off+16])
        ts_us = struct.unpack(">Q", data[off+16:off+24])[0]
        off += 24
        pkt = data[off:off + incl_len]
        off += incl_len
        rec_no += 1
        if len(pkt) < 1:
            continue

        # bit 0 of flags: 0 = sent (host→ctrl), 1 = received (ctrl→host)
        # bit 1: 0 = ACL data, 1 = command/event
        direction = "host->ctrl" if (flags & 1) == 0 else "ctrl->host"
        is_cmd_evt = (flags & 2) != 0
        hci_type = pkt[0]
        body = pkt[1:]

        # HCI Event 0x04 - look for Connection Complete to map handle → addr
        if hci_type == 0x04 and len(body) >= 2:
            event_code = body[0]
            # LE Meta Event = 0x3E, subevent 0x01 = Connection Complete
            if event_code == 0x3E and len(body) >= 21 and body[2] == 0x01:
                status = body[3]
                conn_handle = body[4] | (body[5] << 8)
                # body[6] = role, body[7] = peer_addr_type, body[8..13] = peer_addr (little-endian)
                addr = ":".join(f"{b:02X}" for b in reversed(body[8:14]))
                if status == 0:
                    handle_to_addr[conn_handle] = addr
                    print(f"# {fmt_ts(ts_us)} LE_CONN_COMPLETE handle=0x{conn_handle:04X} peer={addr}")

        # HCI ACL Data 0x02 - L2CAP/ATT payloads live here
        if hci_type == 0x02 and len(body) >= 4:
            hdr = body[0] | (body[1] << 8)
            handle = hdr & 0x0FFF
            pb = (hdr >> 12) & 0x03  # packet boundary: 0=first non-flush, 2=first flushable
            bc = (hdr >> 14) & 0x03
            acl_len = body[2] | (body[3] << 8)
            acl_payload = body[4:4 + acl_len]
            if len(acl_payload) < 4:
                continue
            l2cap_len = acl_payload[0] | (acl_payload[1] << 8)
            l2cap_cid = acl_payload[2] | (acl_payload[3] << 8)
            l2cap_data = acl_payload[4:4 + l2cap_len]
            # Continuation fragment? pb == 1
            if pb == 1:
                # skip continuations (would need reassembly for completeness)
                continue
            if l2cap_cid != 0x0004:  # ATT CID
                continue
            if len(l2cap_data) < 1:
                continue
            opcode = l2cap_data[0]
            opname = ATT_NAMES.get(opcode, f"OP_0x{opcode:02X}")
            mac = handle_to_addr.get(handle, "?")
            if mac_filter and mac.lower() != mac_filter.lower():
                continue
            # For WRITE_REQ / WRITE_CMD / NOTIFY: payload = [handle:2][value...]
            value = b""
            att_handle = None
            if opcode in (0x12, 0x52, 0x1B, 0x1D) and len(l2cap_data) >= 3:
                att_handle = l2cap_data[1] | (l2cap_data[2] << 8)
                value = l2cap_data[3:]

            if opcode in (0x52, 0x12, 0x1B):
                annotation = try_parse_modbus(value) or try_parse_ff55(value) or ""
                arrow = "→" if direction == "host->ctrl" else "←"
                print(f"{fmt_ts(ts_us)}  {arrow}  {opname:9s}  acl=0x{handle:04X} {mac}  "
                      f"att_h=0x{att_handle:04X}  val[{len(value)}]= {value.hex(' ')}  {annotation}")
            elif opcode in (0x09, 0x05, 0x11) and direction == "ctrl->host":
                # Discovery responses — printed only for debugging UUIDs/handles
                print(f"{fmt_ts(ts_us)}  ←  {opname:9s}  acl=0x{handle:04X} {mac}  "
                      f"len={len(l2cap_data)}  {l2cap_data.hex(' ')[:120]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("log", type=Path)
    p.add_argument("--mac", default=None, help="Filter to this peer MAC (e.g. 10:20:BA:42:12:3A)")
    args = p.parse_args()
    parse(args.log, args.mac)
