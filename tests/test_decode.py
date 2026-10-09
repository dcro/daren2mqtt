import pytest
from synthetic import DEFAULT_CELLS, analog_info, analog_reply, device_info

from daren2mqtt.decode import Device, Kind, decode_analog, decode_device
from daren2mqtt.protocol import ProtocolError, decode


def test_values():
    a = decode_analog(decode(analog_reply()).info)
    assert a.soc == pytest.approx(75.5) and a.voltage == pytest.approx(52.85)
    assert a.cells_mv == DEFAULT_CELLS
    assert (a.temp_ambient, a.temp_pack, a.temp_mos) == pytest.approx((24.5, 23.0, 26.5))
    assert a.temp_sensors == pytest.approx((22.0, 22.5, -3.5, 23.0))
    assert a.current == pytest.approx(-12.34)
    assert a.soh == 100 and a.cycles == 42
    assert a.full_ah == pytest.approx(100.0) and a.remaining_ah == pytest.approx(75.5)


def test_derived_values():
    a = decode_analog(analog_info(current=-10.0, voltage=52.0))
    assert a.power == pytest.approx(-520.0)
    assert a.state == "discharging"
    assert a.charge_mos and a.discharge_mos
    assert a.cell_min == (3300, 1) and a.cell_max == (3315, 16)


@pytest.mark.parametrize(("bits", "state"), [(0x0001, "charging"), (0x0002, "discharging"), (0, "idle")])
def test_state(bits, state):
    assert decode_analog(analog_info(status={"current": bits})).state == state


def test_mosfets():
    a = decode_analog(analog_info(status={"fet": 0x0001}))
    assert a.charge_mos and not a.discharge_mos


def test_balancing_cells():
    a = decode_analog(analog_info(balancing=0x0A14 | 1 << 20))
    assert a.balancing_cells == [3, 5, 10, 12, 21]


def test_flags():
    a = decode_analog(analog_info(status={"voltage": 1 << 4 | 1 << 0, "current": 1 << 3, "alarm": 1 << 13}))
    assert a.flags(Kind.ALARM) == ["cell high voltage"]
    assert a.flags(Kind.PROTECTION) == ["cell overvoltage", "short circuit"]
    assert a.flags(Kind.FAULT) == ["temperature sensor"]


def test_state_bits_are_not_problems():
    # charging/discharging, MOSFETs on, current limiter mode, buzzer enable
    a = decode_analog(analog_info(status={"current": 0x0003, "fet": 0x1033, "voltage": 1 << 15}))
    assert not any(a.flags(kind) for kind in Kind)


def test_trailing_fields_are_ignored():
    assert decode_analog(analog_info(tail=b"\x00" * 11)).soc == pytest.approx(75.5)


def test_truncated_reply():
    with pytest.raises(ProtocolError, match="truncated"):
        decode_analog(analog_info()[:-3])


@pytest.mark.parametrize("kwargs", [{"cells": ()}, {"cells": (0,) * 16}, {"soc": 120.0}])
def test_implausible_values(kwargs):
    with pytest.raises(ProtocolError):
        decode_analog(analog_info(**kwargs))


def test_device_information():
    assert decode_device(device_info(firmware=(1, 0, 12), tail=b"\x02\x15")) == Device(
        hardware="HW1", product="ACME01", model="16S100A", firmware="01.00.12"
    )


def test_device_text_fields_are_cleaned():
    info = b"AB  \xff\xff\x00\x00\x00\x00" + b"\x00" * 10 + b"16S100A   " + bytes([1, 2, 3])
    d = decode_device(info)
    assert (d.hardware, d.product, d.model) == ("AB", "", "16S100A")


def test_device_reply_truncated():
    with pytest.raises(ProtocolError, match="51H reply truncated"):
        decode_device(device_info()[:32])
