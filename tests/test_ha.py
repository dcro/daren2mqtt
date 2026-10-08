import pytest
from synthetic import analog_info

from daren2mqtt import ha
from daren2mqtt.config import MqttConfig, PackConfig
from daren2mqtt.decode import decode_analog

PACK = PackConfig(id="battery1", name="Battery 1", host="192.0.2.10")


def test_state_uses_inverter_sign_convention():
    s = ha.state(decode_analog(analog_info(current=-10.0, voltage=52.0)))
    assert s["current"] == pytest.approx(10.0) and s["power"] == pytest.approx(520.0)
    assert s["state"] == "discharging"


def test_state_without_current_has_no_negative_zero():
    s = ha.state(decode_analog(analog_info(current=0.0, status={"current": 0})))
    assert str(s["current"]) == "0.0" and str(s["power"]) == "0.0"


def test_state_values():
    s = ha.state(decode_analog(analog_info(balancing=0b101)))
    assert s["cell_1"] == pytest.approx(3.300) and s["cell_16"] == pytest.approx(3.315)
    assert (s["cell_min"], s["cell_max"], s["cell_delta"]) == pytest.approx((3.300, 3.315, 15))
    assert (s["cell_min_index"], s["cell_max_index"]) == (1, 16)
    assert s["temperature_4"] == pytest.approx(23.0) and "temperature_5" not in s
    assert s["balancing"] is True and s["balancing_cells"] == "1, 3"
    assert (s["alarms"], s["protections"], s["faults"], s["problem"]) == ("OK", "OK", "OK", False)


def test_state_problems():
    s = ha.state(decode_analog(analog_info(status={"voltage": 1 << 0})))
    assert s["protections"] == "cell overvoltage" and s["problem"] is True


def test_discovery_matches_state_keys():
    analog = decode_analog(analog_info())
    d = ha.discovery(MqttConfig(), PACK, len(analog.cells_mv), len(analog.temp_sensors))
    assert set(d["cmps"]) == set(ha.state(analog))
    assert d["state_topic"] == "daren2mqtt/battery1"
    assert d["availability"] == [
        {"topic": "daren2mqtt/bridge/state"},
        {"topic": "daren2mqtt/battery1/availability"},
    ]
    assert d["dev"]["identifiers"] == ["daren2mqtt_battery1"] and d["dev"]["name"] == "Battery 1"
    uids = [c["unique_id"] for c in d["cmps"].values()]
    assert len(set(uids)) == len(uids)


def test_discovery_components():
    cmps = ha.discovery(MqttConfig(), PACK, 16, 4)["cmps"]
    assert cmps["soc"] == {
        "p": "sensor",
        "name": "State of charge",
        "unit_of_measurement": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "suggested_display_precision": 0,
        "unique_id": "daren2mqtt_battery1_soc",
        "value_template": "{{ value_json.soc }}",
    }
    assert cmps["problem"]["value_template"] == "{{ 'ON' if value_json.problem else 'OFF' }}"
    assert cmps["problem"]["device_class"] == "problem"


def test_topics():
    t = ha.topics(MqttConfig(base_topic="bms", discovery_prefix="ha"), PACK)
    assert t == {
        "state": "bms/battery1",
        "availability": "bms/battery1/availability",
        "bridge": "bms/bridge/state",
        "discovery": "ha/device/daren2mqtt_battery1/config",
    }
