"""YD/T 1363 ASCII framing as used by Daren BMS boards on their RS485 PC port.

A frame is ``~`` + ASCII hex of VER ADR CID1 CID2 LENGTH INFO + CHKSUM + ``\\r``.
Only read requests are built here; nothing in this package can write to a BMS.
"""

from dataclasses import dataclass

SOI = 0x7E  # '~'
EOI = 0x0D  # '\r'
VER = 0x22
CID1_BMS = 0x4A  # device type: LiFePO4 battery management system
CID2_ANALOG = 0x42  # read analog values and status
CID2_DEVICE = 0x51  # read device information (model, hardware, firmware)
CID2_MODULE = 0xB0  # read or write a parameter module; only reads are built here
CID2_THRESHOLDS = 0x80  # read the protection and alarm thresholds
CID2_PROTECTION_COUNTS = 0x83  # read or clear the protection counters; only reads are built here

RETURN_CODES = {
    0x00: "OK",
    0x01: "version error",
    0x02: "checksum error",
    0x03: "length checksum error",
    0x04: "invalid command",
    0x05: "command format error",
    0x06: "invalid data",
    0x90: "address error",
    0x91: "communication error",
}


class ProtocolError(ValueError):
    """A frame that is malformed, fails a checksum or carries an error code."""


@dataclass(frozen=True, slots=True)
class Frame:
    ver: int
    adr: int
    cid1: int
    cid2: int  # command in a request, return code (RTN) in a response
    info: bytes  # INFO decoded from ASCII hex


def length_field(info_chars: int) -> int:
    """LENGTH: 12-bit count of INFO ASCII characters with a 4-bit checksum on top."""
    if not 0 <= info_chars <= 0xFFF:
        raise ProtocolError(f"INFO too long: {info_chars} characters")
    nibbles = (info_chars & 0xF) + ((info_chars >> 4) & 0xF) + ((info_chars >> 8) & 0xF)
    return ((-nibbles & 0xF) << 12) | info_chars


def checksum(body: bytes) -> int:
    """Two's complement of the byte sum of everything between SOI and CHKSUM."""
    return -sum(body) & 0xFFFF


def encode(adr: int, cid2: int, info: bytes = b"", cid1: int = CID1_BMS, ver: int = VER) -> bytes:
    info_hex = info.hex().upper().encode()
    body = b"%02X%02X%02X%02X%04X" % (ver, adr, cid1, cid2, length_field(len(info_hex))) + info_hex
    return bytes([SOI]) + body + b"%04X" % checksum(body) + bytes([EOI])


def decode(frame: bytes) -> Frame:
    if len(frame) < 18 or frame[0] != SOI or frame[-1] != EOI:
        raise ProtocolError(f"not a frame: {frame[:20]!r}")
    body, chk = frame[1:-5], frame[-5:-1]
    try:
        received = int(chk, 16)
        ver, adr, cid1, cid2, length = (
            int(body[i : i + n], 16) for i, n in ((0, 2), (2, 2), (4, 2), (6, 2), (8, 4))
        )
        info = bytes.fromhex(body[12:].decode("ascii"))
    except ValueError as exc:
        raise ProtocolError(f"frame is not valid hex: {frame[:20]!r}") from exc
    if received != checksum(body):
        raise ProtocolError(f"checksum mismatch: got {received:04X}, want {checksum(body):04X}")
    if length != length_field(len(body) - 12):
        raise ProtocolError(f"LENGTH field {length:04X} does not match {len(body) - 12} INFO characters")
    return Frame(ver, adr, cid1, cid2, info)


def _check_address(address: int) -> None:
    if not 0 <= address <= 15:
        raise ValueError(f"pack address must be 0..15, got {address}")


def analog_request(address: int) -> bytes:
    """Read analog values and status of the pack at ``address`` (the DIP switch value).

    The command group in INFO is the pack address, as the vendor's PC tool sends it.
    """
    _check_address(address)
    return encode(address, CID2_ANALOG, bytes([address]))


def device_request(address: int) -> bytes:
    """Read the device information of the pack at ``address``; the request has no INFO."""
    _check_address(address)
    return encode(address, CID2_DEVICE)


def counters_request(address: int) -> bytes:
    """Read the lifetime capacity and energy counters (module 4) of the pack at ``address``.

    INFO: command group (the address), operation 01 (read), module 04, function FF (all
    fields), function length 00.
    """
    _check_address(address)
    return encode(address, CID2_MODULE, bytes([address, 0x01, 0x04, 0xFF, 0x00]))


def thresholds_request(address: int) -> bytes:
    """Read the protection, alarm and balancing thresholds of the pack at ``address``."""
    _check_address(address)
    return encode(address, CID2_THRESHOLDS, bytes([address]))


def protection_counts_request(address: int) -> bytes:
    """Read how often each protection has tripped; INFO: command group, operation 01 (read)."""
    _check_address(address)
    return encode(address, CID2_PROTECTION_COUNTS, bytes([address, 0x01]))
