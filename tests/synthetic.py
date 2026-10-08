"""Synthetic 42H replies built from the documented layout (no captured data)."""

from daren2mqtt.protocol import encode

DEFAULT_CELLS = tuple(3300 + i for i in range(16))


def _u16(value: int) -> bytes:
    return (value & 0xFFFF).to_bytes(2, "big")


def analog_info(
    cells=DEFAULT_CELLS,
    soc=75.5,
    voltage=52.85,
    temps=(24.5, 23.0, 26.5),
    sensors=(22.0, 22.5, -3.5, 23.0),
    current=-12.34,
    soh=100,
    full_ah=100.0,
    remaining_ah=75.5,
    cycles=42,
    status=None,
    balancing=0,
    tail=b"",
) -> bytes:
    status = {"voltage": 0, "current": 0x0002, "temperature": 0, "alarm": 0, "fet": 0x0003} | (status or {})
    out = bytes([0]) + _u16(round(soc * 100)) + _u16(round(voltage * 100)) + bytes([len(cells)])
    out += b"".join(_u16(c) for c in cells)
    out += b"".join(_u16(round(t * 10)) for t in temps)
    out += bytes([len(sensors)]) + b"".join(_u16(round(t * 10)) for t in sensors)
    out += _u16(round(current * 100)) + _u16(0) + _u16(soh) + bytes([1])
    out += _u16(round(full_ah * 100)) + _u16(round(remaining_ah * 100)) + _u16(cycles)
    out += b"".join(_u16(status[w]) for w in ("voltage", "current", "temperature", "alarm", "fet"))
    out += _u16(0) * 4 + _u16(balancing & 0xFFFF) + _u16(balancing >> 16)
    return out + tail


def analog_reply(address: int = 0, rtn: int = 0x00, **kwargs) -> bytes:
    return encode(address, rtn, analog_info(**kwargs))
