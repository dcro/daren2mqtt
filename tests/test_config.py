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
    assert (m.host, m.port, m.username) == ("mosquitto", 1884, "yaml")
    assert m.password.get_secret_value() == "pw" and "pw" not in repr(m)


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
        "positive_current: in\npacks:\n  - {id: a, host: h}",
        "packs:\n  - {id: a}",
        "packs:\n  - {id: a, host: h, serial: /dev/ttyUSB0}",
        "packs:\n  - {id: a, serial: /dev/ttyUSB0}\n  - {id: b, serial: /dev/ttyUSB0}",
        "packs:\n  - {id: a, serial: /dev/ttyS0}\n  - {id: b, serial: /dev/ttyS0, address: 1, baud: 2400}",
        "packs:\n  - {id: a, serial: /dev/ttyUSB0, baud: 300}",
        "- not a mapping",
        "packs: [",
        "mqtt: {base_topic: 'bms/#'}\npacks:\n  - {id: a, host: h}",
        "mqtt: {discovery_prefix: 'home assistant'}\npacks:\n  - {id: a, host: h}",
        "mqtt: {base_topic: 'bms/'}\npacks:\n  - {id: a, host: h}",
        "mqtt: {client_id: ''}\npacks:\n  - {id: a, host: h}",
    ],
)
def test_invalid_config(text):
    with pytest.raises(ConfigError):
        load({"DAREN2MQTT_CONFIG": text})


@pytest.mark.parametrize(
    "mqtt",
    [
        "password: 123456789",  # a number, not a string
        "passwd: 123456789",  # unknown key
        "password: [123456789]",
        'password: "123456789"x',  # YAML syntax error on the password line
    ],
)
def test_errors_never_show_the_password(mqtt):
    with pytest.raises(ConfigError) as info:
        load({"DAREN2MQTT_CONFIG": f"mqtt:\n  {mqtt}\n" + PACKS})
    assert "123456789" not in str(info.value)


def test_numeric_password_hint():
    with pytest.raises(ConfigError, match=r"mqtt\.password: .*put the value in quotes"):
        load({"DAREN2MQTT_CONFIG": "mqtt:\n  password: 1234\n" + PACKS})


def test_nested_base_topic():
    assert load({"DAREN2MQTT_CONFIG": "mqtt: {base_topic: home/bms}\n" + PACKS}).mqtt.base_topic == "home/bms"


def test_positive_current():
    assert load({"DAREN2MQTT_CONFIG": PACKS}).positive_current == "charging"
    config = load({"DAREN2MQTT_CONFIG": "positive_current: discharging\n" + PACKS})
    assert config.positive_current == "discharging"


SERIAL_PACKS = """
packs:
  - {id: a, serial: /dev/ttyUSB0}
  - {id: b, serial: /dev/ttyUSB0, address: 1}
"""


def test_serial_packs():
    c = load({"DAREN2MQTT_CONFIG": SERIAL_PACKS})
    assert [(p.serial, p.baud, p.bus) for p in c.packs] == [
        ("/dev/ttyUSB0", 9600, ("serial", "/dev/ttyUSB0")),
        ("/dev/ttyUSB0", 9600, ("serial", "/dev/ttyUSB0")),
    ]
