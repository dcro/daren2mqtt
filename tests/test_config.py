import pytest

from daren2mqtt.config import ConfigError, load

PACKS = """
packs:
  - id: battery1
    name: Battery 1
    host: 192.0.2.10
  - id: battery2
    host: 192.0.2.11
    address: 1
"""


def test_inline_config_with_defaults():
    c = load({"DAREN2MQTT_CONFIG": PACKS})
    assert c.interval == 30 and c.timeout == 2
    assert c.mqtt.host == "localhost" and c.mqtt.base_topic == "daren2mqtt"
    assert [(p.id, p.display_name, p.port, p.address) for p in c.packs] == [
        ("battery1", "Battery 1", 4196, 0),
        ("battery2", "battery2", 4196, 1),
    ]


def test_mqtt_environment_overrides_yaml():
    yaml = PACKS + "mqtt:\n  host: broker\n  username: yaml\n"
    env = {"DAREN2MQTT_CONFIG": yaml, "MQTT_HOST": "mosquitto", "MQTT_PORT": "1884", "MQTT_PASSWORD": "pw"}
    m = load(env).mqtt
    assert (m.host, m.port, m.username, m.password) == ("mosquitto", 1884, "yaml", "pw")


def test_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(PACKS)
    assert len(load({"DAREN2MQTT_CONFIG_FILE": str(path)}).packs) == 2


def test_missing_config(tmp_path):
    with pytest.raises(ConfigError, match="no configuration"):
        load({"DAREN2MQTT_CONFIG_FILE": str(tmp_path / "missing.yaml")})


@pytest.mark.parametrize(
    "text",
    [
        "packs: []",
        "packs:\n  - {id: a, host: h}\n  - {id: a, host: h, address: 1}",
        "packs:\n  - {id: a, host: h}\n  - {id: b, host: h}",
        "packs:\n  - {id: a, host: h, address: 16}",
        "packs:\n  - {id: 'a b', host: h}",
        "packs:\n  - {id: a, host: h, adress: 1}",
        "interval: 1\npacks:\n  - {id: a, host: h}",
        "- not a mapping",
        "packs: [",
    ],
)
def test_invalid_config(text):
    with pytest.raises(ConfigError):
        load({"DAREN2MQTT_CONFIG": text})
