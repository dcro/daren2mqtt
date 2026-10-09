"""Configuration: YAML (inline in DAREN2MQTT_CONFIG or a file), MQTT_* environment overrides."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

DEFAULT_FILE = "/config/config.yaml"
# MQTT topic levels without wildcards, spaces or empty levels, e.g. "home/daren2mqtt"
TOPIC = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")]
MQTT_ENV = {
    "MQTT_HOST": "host",
    "MQTT_PORT": "port",
    "MQTT_USERNAME": "username",
    "MQTT_PASSWORD": "password",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MqttConfig(_Strict):
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: SecretStr | None = None
    client_id: Annotated[str, Field(min_length=1)] = "daren2mqtt"
    base_topic: TOPIC = "daren2mqtt"
    discovery_prefix: TOPIC = "homeassistant"


class PackConfig(_Strict):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
    name: str | None = None
    host: str
    port: int = 4196
    address: Annotated[int, Field(ge=0, le=15)] = 0

    @property
    def display_name(self) -> str:
        return self.name or self.id


class Config(_Strict):
    interval: Annotated[float, Field(ge=5)] = 30
    timeout: Annotated[float, Field(gt=0, le=10)] = 2
    log_level: str = "INFO"
    positive_current: Literal["charging", "discharging"] = "charging"
    mqtt: MqttConfig = MqttConfig()
    packs: Annotated[list[PackConfig], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self):
        ids = [p.id for p in self.packs]
        if len(set(ids)) != len(ids):
            raise ValueError("pack ids must be unique")
        targets = [(p.host, p.port, p.address) for p in self.packs]
        if len(set(targets)) != len(targets):
            raise ValueError("two packs use the same host, port and address")
        return self


class ConfigError(Exception):
    pass


def _validation_message(exc: ValidationError) -> str:
    """One line per error, without the offending input: it may be a password."""
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in err["loc"]) or "config"
        hint = " (put the value in quotes)" if err["type"] == "string_type" else ""
        lines.append(f"{where}: {err['msg']}{hint}")
    return "\n  ".join(lines)


def _yaml_message(exc: yaml.YAMLError) -> str:
    """Position and reason only; PyYAML would otherwise quote the line, secrets included."""
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return f"invalid YAML{where}: {getattr(exc, 'problem', None) or 'syntax error'}"


def load(env: Mapping[str, str] = os.environ) -> Config:
    if inline := env.get("DAREN2MQTT_CONFIG"):
        source, text = "DAREN2MQTT_CONFIG", inline
    else:
        path = Path(env.get("DAREN2MQTT_CONFIG_FILE", DEFAULT_FILE))
        if not path.is_file():
            raise ConfigError(f"no configuration: set DAREN2MQTT_CONFIG or provide {path}")
        source, text = str(path), path.read_text()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: {_yaml_message(exc)}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    mqtt = data.get("mqtt") or {}
    if isinstance(mqtt, dict):
        mqtt |= {key: env[var] for var, key in MQTT_ENV.items() if env.get(var)}
    data["mqtt"] = mqtt
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}:\n  {_validation_message(exc)}") from None
