"""MQTT payloads: the JSON state of a pack and its Home Assistant device discovery.

Current and power follow the inverter convention: positive while discharging.
"""

from daren2mqtt import __version__
from daren2mqtt.config import MqttConfig, PackConfig
from daren2mqtt.decode import Analog, Kind

MAX_TEXT = 255  # Home Assistant rejects longer sensor states


def _text(items: list[str], empty: str) -> str:
    return ", ".join(items)[:MAX_TEXT] or empty


def state(a: Analog) -> dict:
    low, low_index = a.cell_min
    high, high_index = a.cell_max
    problems = {kind: a.flags(kind) for kind in Kind}
    return {
        "soc": a.soc,
        "voltage": a.voltage,
        "current": round(-a.current, 2) + 0.0,  # + 0.0 turns -0.0 into 0.0
        "power": round(-a.power, 1) + 0.0,
        "state": a.state,
        "soh": a.soh,
        "remaining_capacity": a.remaining_ah,
        "full_capacity": a.full_ah,
        "cycles": a.cycles,
        **{f"cell_{i}": mv / 1000 for i, mv in enumerate(a.cells_mv, 1)},
        "cell_min": low / 1000,
        "cell_max": high / 1000,
        "cell_delta": high - low,
        "cell_min_index": low_index,
        "cell_max_index": high_index,
        "temperature_ambient": a.temp_ambient,
        "temperature_pack": a.temp_pack,
        "temperature_mos": a.temp_mos,
        **{f"temperature_{i}": t for i, t in enumerate(a.temp_sensors, 1)},
        "charge_mos": a.charge_mos,
        "discharge_mos": a.discharge_mos,
        "balancing": bool(a.balancing_cells),
        "balancing_cells": _text([str(c) for c in a.balancing_cells], "none"),
        "alarms": _text(problems[Kind.ALARM], "OK"),
        "protections": _text(problems[Kind.PROTECTION], "OK"),
        "faults": _text(problems[Kind.FAULT], "OK"),
        "problem": bool(problems[Kind.PROTECTION] or problems[Kind.FAULT]),  # alarms are only warnings
    }


def _measure(name, unit=None, device_class=None, precision=None, state_class="measurement", **extra):
    out = {"p": "sensor", "name": name, "unit_of_measurement": unit, "device_class": device_class}
    out |= {"state_class": state_class, "suggested_display_precision": precision, **extra}
    return {k: v for k, v in out.items() if v is not None}


def _volts(name, **extra):
    return _measure(name, "V", "voltage", 3, **extra)


def _temp(name, **extra):
    return _measure(name, "°C", "temperature", 1, **extra)


def _text_sensor(name, **extra):
    return {"p": "sensor", "name": name, **extra}


def _binary(name, device_class=None, **extra):
    out = {"p": "binary_sensor", "name": name, "device_class": device_class, **extra}
    return {k: v for k, v in out.items() if v is not None}


DIAGNOSTIC = {"entity_category": "diagnostic"}


def components(cells: int, sensors: int) -> dict[str, dict]:
    return {
        "soc": _measure("State of charge", "%", "battery", 0),
        "voltage": _measure("Voltage", "V", "voltage", 2),
        "current": _measure("Current", "A", "current", 2),
        "power": _measure("Power", "W", "power", 0),
        "state": _text_sensor("State", device_class="enum", options=["charging", "discharging", "idle"]),
        "soh": _measure("State of health", "%", precision=0),
        "remaining_capacity": _measure("Remaining capacity", "Ah", precision=1, icon="mdi:battery-clock"),
        "full_capacity": _measure("Full capacity", "Ah", precision=1, icon="mdi:battery", **DIAGNOSTIC),
        "cycles": _measure("Cycles", state_class="total_increasing", icon="mdi:battery-sync"),
        **{f"cell_{i}": _volts(f"Cell {i} voltage") for i in range(1, cells + 1)},
        "cell_min": _volts("Cell voltage min"),
        "cell_max": _volts("Cell voltage max"),
        "cell_delta": _measure("Cell voltage delta", "mV", "voltage", 0),
        "cell_min_index": _text_sensor("Cell min", icon="mdi:battery-arrow-down", **DIAGNOSTIC),
        "cell_max_index": _text_sensor("Cell max", icon="mdi:battery-arrow-up", **DIAGNOSTIC),
        "temperature_ambient": _temp("Ambient temperature"),
        "temperature_pack": _temp("Pack temperature"),
        "temperature_mos": _temp("MOS temperature"),
        **{f"temperature_{i}": _temp(f"Temperature {i}") for i in range(1, sensors + 1)},
        "charge_mos": _binary("Charge MOS", "power", **DIAGNOSTIC),
        "discharge_mos": _binary("Discharge MOS", "power", **DIAGNOSTIC),
        "balancing": _binary("Balancing", icon="mdi:scale-balance"),
        "balancing_cells": _text_sensor("Balancing cells", icon="mdi:scale-balance"),
        "alarms": _text_sensor("Alarms", icon="mdi:alert"),
        "protections": _text_sensor("Protections", icon="mdi:shield-alert"),
        "faults": _text_sensor("Faults", icon="mdi:alert-octagon"),
        "problem": _binary("Problem", "problem"),
    }


def topics(mqtt: MqttConfig, pack: PackConfig) -> dict[str, str]:
    base = f"{mqtt.base_topic}/{pack.id}"
    return {
        "state": base,
        "availability": f"{base}/availability",
        "bridge": f"{mqtt.base_topic}/bridge/state",
        "discovery": f"{mqtt.discovery_prefix}/device/daren2mqtt_{pack.id}/config",
    }


def discovery(mqtt: MqttConfig, pack: PackConfig, cells: int, sensors: int) -> dict:
    t = topics(mqtt, pack)
    uid = f"daren2mqtt_{pack.id}"
    cmps = {}
    for key, cfg in components(cells, sensors).items():
        template = f"{{{{ value_json.{key} }}}}"
        if cfg["p"] == "binary_sensor":
            template = f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}"
        cmps[key] = {**cfg, "unique_id": f"{uid}_{key}", "value_template": template}
    return {
        "dev": {"identifiers": [uid], "name": pack.display_name, "manufacturer": "Daren", "model": "BMS"},
        "o": {
            "name": "daren2mqtt",
            "sw_version": __version__,
            "support_url": "https://github.com/dcro/daren2mqtt",
        },
        "cmps": cmps,
        "state_topic": t["state"],
        "availability": [{"topic": t["bridge"]}, {"topic": t["availability"]}],
        "availability_mode": "all",
        "qos": 1,
    }
