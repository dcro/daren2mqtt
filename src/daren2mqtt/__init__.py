"""Daren BMS to MQTT bridge."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("daren2mqtt")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"
