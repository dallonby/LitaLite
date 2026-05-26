"""Modbus RTU helpers for the Wendougee BLE protocol.

CRC16-Modbus (poly reflected 0xA001, init 0xFFFF, no final xor, little-endian transmit),
plus builders for the function codes the espresso machine speaks.

Every builder returns the full on-the-wire bytes (slave_addr ... CRC), ready to
hand to a BLE write. parse_response() validates CRC and returns a structured view.
"""

from __future__ import annotations

from dataclasses import dataclass


SLAVE_ADDR = 0x01


def crc16_modbus(data: bytes) -> int:
    """Standard CRC16-Modbus. Returns the integer CRC; transmit low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _wrap(payload: bytes) -> bytes:
    crc = crc16_modbus(payload)
    return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_read_coils(addr: int, qty: int, slave: int = SLAVE_ADDR) -> bytes:
    return _wrap(bytes([slave, 0x01]) + addr.to_bytes(2, "big") + qty.to_bytes(2, "big"))


def build_read_holding(addr: int, qty: int, slave: int = SLAVE_ADDR) -> bytes:
    return _wrap(bytes([slave, 0x03]) + addr.to_bytes(2, "big") + qty.to_bytes(2, "big"))


def build_write_coil(addr: int, on: bool, slave: int = SLAVE_ADDR) -> bytes:
    val = b"\xff\x00" if on else b"\x00\x00"
    return _wrap(bytes([slave, 0x05]) + addr.to_bytes(2, "big") + val)


def build_write_register(addr: int, value: int, slave: int = SLAVE_ADDR) -> bytes:
    return _wrap(bytes([slave, 0x06]) + addr.to_bytes(2, "big") + value.to_bytes(2, "big"))


def build_write_registers(addr: int, values: list[int], slave: int = SLAVE_ADDR) -> bytes:
    qty = len(values)
    payload = bytes([slave, 0x10]) + addr.to_bytes(2, "big") + qty.to_bytes(2, "big") + bytes([qty * 2])
    for v in values:
        payload += v.to_bytes(2, "big")
    return _wrap(payload)


@dataclass
class Response:
    slave: int
    function: int
    data: bytes
    raw: bytes


def parse_response(raw: bytes) -> Response | None:
    """Validate CRC and return a Response, or None on CRC failure / short frame."""
    if len(raw) < 4:
        return None
    body, crc_bytes = raw[:-2], raw[-2:]
    expected_crc = crc_bytes[0] | (crc_bytes[1] << 8)
    if crc16_modbus(body) != expected_crc:
        return None
    return Response(slave=body[0], function=body[1], data=body[2:], raw=raw)


# Self-test against the verified packets pulled from libapp.so.
KNOWN_PACKETS: list[tuple[str, bytes]] = [
    ("read 37 holding from 0",      bytes.fromhex("0103000000258411")),
    ("read 20 holding from 1404",   bytes.fromhex("0103057C001484D1")),
    ("read 22 holding from 1404",   bytes.fromhex("0103057C00160510")),
    ("read 16 holding from 387",    bytes.fromhex("010301830010B412")),
    ("read 1 holding from 1441",    bytes.fromhex("010305A10001D524")),
    ("read 7 coils from 182",       bytes.fromhex("010100B600079C2E")),
    ("read 8 coils from 193",       bytes.fromhex("010100C100086C30")),
    ("coil 150 ON (start brew)",    bytes.fromhex("01050096FF006C16")),
    ("coil 150 OFF",                bytes.fromhex("0105009600002DE6")),
    ("coil 154 ON",                 bytes.fromhex("0105009AFF00AC15")),
    ("coil 154 OFF",                bytes.fromhex("0105009A0000EDE5")),
    ("coil 155 ON",                 bytes.fromhex("0105009BFF00FDD5")),
    ("coil 155 OFF",                bytes.fromhex("0105009B0000BC25")),
    ("coil 157 ON (test)",          bytes.fromhex("0105009DFF001DD4")),
    ("coil 157 OFF",                bytes.fromhex("0105009D00005C24")),
    ("reg 15 <- 4 (active model)",  bytes.fromhex("0106000F0004B80A")),
    ("reg 1459 <- 1",               bytes.fromhex("010605B30001B921")),
    ("reg 1459 <- 0",               bytes.fromhex("010605B3000078E1")),
]


def _selftest() -> None:
    failures = 0
    for desc, pkt in KNOWN_PACKETS:
        body, crc_bytes = pkt[:-2], pkt[-2:]
        expected = crc_bytes[0] | (crc_bytes[1] << 8)
        actual = crc16_modbus(body)
        ok = actual == expected
        print(f"  {'ok ' if ok else 'FAIL'}  {pkt.hex().upper():<22}  {desc}")
        if not ok:
            failures += 1
            print(f"           expected CRC {expected:04X}, computed {actual:04X}")
    print(f"\n{len(KNOWN_PACKETS) - failures}/{len(KNOWN_PACKETS)} packets verified")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    print("CRC16-Modbus self-test against packets extracted from libapp.so:\n")
    _selftest()
