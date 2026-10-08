"""Configuration: YAML (inline in DAREN2MQTT_CONFIG or a file), MQTT_* environment overrides."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_FILE = "/config/config.yaml"
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
    password: str | None = None
    client_id: str = "daren2mqtt"
    base_topic: str = "daren2mqtt"
    discovery_prefix: str = "homeassistant"


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
        raise ConfigError(f"{source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    mqtt = data.setdefault("mqtt", {}) or {}
    for var, key in MQTT_ENV.items():
        if env.get(var):
            mqtt[key] = env[var]
    data["mqtt"] = mqtt
    try:
        return Config.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
