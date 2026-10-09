import pytest

from daren2mqtt.protocol import (
    ProtocolError,
    analog_request,
    checksum,
    counters_request,
    decode,
    device_request,
    encode,
    length_field,
)


def test_analog_request_bytes():
    assert analog_request(0) == b"~22004A42E00200FD2A\r"
    assert analog_request(1) == b"~22014A42E00201FD28\r"


def test_analog_request_rejects_addresses_outside_dip_range():
    for address in (-1, 16):
        with pytest.raises(ValueError):
            analog_request(address)


def test_standard_examples():
    # YD/T 1363 worked examples: INFO of 18 characters, and a complete frame
    assert length_field(18) == 0xD012
    assert checksum(b"20014043E00200") == 0xFD3B


def test_round_trip():
    frame = decode(encode(3, 0x00, b"\x01\x02\xff"))
    assert (frame.ver, frame.adr, frame.cid1, frame.cid2) == (0x22, 3, 0x4A, 0x00)
    assert frame.info == b"\x01\x02\xff"


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b">22004A42E00200FD2A\r",  # wrong start byte
        b"~22004A42E00200FD2B\r",  # checksum
        b"~22004A42E00100FD2B\r",  # LENGTH does not match INFO
        b"~22004A42E002ZZFD2A\r",  # not hex
        b"~22004A42E00200FD2A",  # no end byte
    ],
)
def test_decode_rejects_bad_frames(frame):
    with pytest.raises(ProtocolError):
        decode(frame)


def test_device_request_bytes():
    # no INFO: LENGTH is 0000
    assert device_request(1) == b"~22014A510000FDA0\r"
    with pytest.raises(ValueError):
        device_request(16)


def test_counters_request_bytes():
    assert counters_request(1) == b"~22014AB0600A010104FF00FB6B\r"
    assert counters_request(0) == b"~22004AB0600A000104FF00FB6D\r"
