import pytest
from synthetic import analog_info

from daren2mqtt import ha
from daren2mqtt.config import MqttConfig, PackConfig
from daren2mqtt.decode import Counters, Device, ProtectionCounts, Thresholds, decode_analog

PACK = PackConfig(id="battery1", name="Battery 1", host="192.0.2.10")


def test_state_uses_the_bms_sign_by_default():
    s = ha.state(decode_analog(analog_info(current=-10.0, voltage=52.0)))
    assert s["current"] == pytest.approx(-10.0) and s["power"] == pytest.approx(-520.0)
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


def test_alarms_alone_are_not_a_problem():
    s = ha.state(decode_analog(analog_info(status={"voltage": 1 << 4})))
    assert s["alarms"] == "cell high voltage" and s["problem"] is False


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
    assert cmps["cell_16"]["name"] == "Cell voltage 16" and "cell_17" not in cmps
    assert cmps["temperature_4"]["name"] == "Cell temperature 4"
    assert "entity_category" not in cmps["charge_mos"]


def test_discovery_device_model():
    assert ha.discovery(MqttConfig(), PACK, 16, 4)["dev"]["model"] == "16S BMS"


def test_topics():
    t = ha.topics(MqttConfig(base_topic="bms", discovery_prefix="ha"), PACK)
    assert t == {
        "state": "bms/battery1",
        "availability": "bms/battery1/availability",
        "bridge": "bms/bridge/state",
        "discovery": "ha/device/daren2mqtt_battery1/config",
    }


def test_discovery_device_information():
    device = Device(hardware="HW1", product="ACME01", model="16S100A", firmware="01.02.03")
    dev = ha.discovery(MqttConfig(), PACK, 16, 4, device)["dev"]
    assert (dev["model"], dev["hw_version"], dev["sw_version"]) == ("16S100A", "HW1", "01.02.03")
    unnamed = Device(hardware="", product="", model="", firmware="01.02.03")
    dev = ha.discovery(MqttConfig(), PACK, 16, 4, unnamed)["dev"]
    assert dev["model"] == "16S BMS" and "hw_version" not in dev


def test_counters_in_state_and_discovery():
    counters = Counters(design_ah=280.0, charged_ah=10, discharged_ah=9, charged_kwh=0.5, discharged_kwh=0.4)
    s = ha.state(decode_analog(analog_info()), {"counters": counters})
    assert (s["charged_energy"], s["discharged_energy"], s["design_capacity"]) == (0.5, 0.4, 280.0)
    assert "charged_energy" not in ha.state(decode_analog(analog_info()))
    cmps = ha.discovery(MqttConfig(), PACK, 16, 4, extras=["counters"])["cmps"]
    assert (
        cmps["charged_energy"]["device_class"] == "energy"
        and cmps["charged_energy"]["unit_of_measurement"] == "kWh"
    )
    assert cmps["design_capacity"]["entity_category"] == "diagnostic"
    assert "charged_energy" not in ha.discovery(MqttConfig(), PACK, 16, 4)["cmps"]


def test_positive_current_while_discharging():
    a = decode_analog(analog_info(current=-10.0, voltage=52.0))
    s = ha.state(a, positive_current="discharging")
    assert s["current"] == pytest.approx(10.0) and s["power"] == pytest.approx(520.0)


def test_entity_categories():
    cmps = ha.discovery(MqttConfig(), PACK, 16, 4, extras=list(ha.EXTRAS))["cmps"]
    diagnostic = {k for k, c in cmps.items() if c.get("entity_category") == "diagnostic"}
    assert diagnostic == {
        "design_capacity",
        "cell_min_index",
        "cell_max_index",
        "balancing_cells",
        "alarms",
        "protections",
        "faults",
        *ha.EXTRAS["protection_counts"][1],
        *ha.EXTRAS["thresholds"][1],
    }


def test_protection_counts_and_thresholds_in_state():
    extras = {
        "protection_counts": ProtectionCounts(41, 3, 1, 0, 2),
        "thresholds": Thresholds(3650, 2600, 3600, 2800, 3400, 20),
    }
    s = ha.state(decode_analog(analog_info()), extras)
    assert s["overcharge_protections"] == 41 and s["short_circuit_protections"] == 2
    assert s["cell_overvoltage_protection"] == 3.65 and s["cell_low_voltage_alarm"] == 2.8
    assert s["balancing_start_voltage"] == 3.4 and s["balancing_delta"] == 20
    cmps = ha.discovery(MqttConfig(), PACK, 16, 4, extras=list(extras))["cmps"]
    assert set(ha.EXTRAS["thresholds"][1]) <= set(cmps)
    assert cmps["overcharge_protections"]["state_class"] == "total_increasing"
    assert "state_class" not in cmps["cell_overvoltage_protection"]  # a setting, not a measurement
