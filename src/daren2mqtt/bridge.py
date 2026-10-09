"""Polls every pack and publishes its state, availability and discovery over MQTT."""

import asyncio
import contextlib
import json
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiomqtt

from daren2mqtt import ha
from daren2mqtt.config import Config, PackConfig
from daren2mqtt.decode import (
    Analog,
    Device,
    decode_analog,
    decode_counters,
    decode_device,
    decode_protection_counts,
    decode_thresholds,
)
from daren2mqtt.link import Link, SerialLink, TcpLink
from daren2mqtt.protocol import (
    ProtocolError,
    analog_request,
    counters_request,
    device_request,
    protection_counts_request,
    thresholds_request,
)

log = logging.getLogger(__name__)

OFFLINE_AFTER = 3  # consecutive failed reads before a pack is reported unavailable
TRIES = 3  # attempts at an optional reading before doing without it
MQTT_RETRY = 10  # seconds
HEARTBEAT = 15  # seconds between touches of the health file while connected to MQTT
HEALTH_FILE = Path(tempfile.gettempdir()) / "daren2mqtt.health"

Publish = Callable[[str, str, bool], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ExtraReading:
    """A reading beyond 42H that a BMS may not support."""

    name: str
    description: str
    request: Callable[[int], bytes]
    decode: Callable[[bytes], object]
    every_poll: bool  # otherwise read once, at the first poll


def _device_summary(d: Device) -> str:
    return f"model {d.model or '?'}, hardware {d.hardware or '?'}, firmware {d.firmware}"


# Read in this order after each 42H; the ones read once come first.
EXTRA_READINGS = (
    ExtraReading("device", "device information", device_request, decode_device, every_poll=False),
    ExtraReading("thresholds", "thresholds", thresholds_request, decode_thresholds, every_poll=False),
    ExtraReading("counters", "energy counters", counters_request, decode_counters, every_poll=True),
    ExtraReading(
        "protection_counts", "protection counts", protection_counts_request, decode_protection_counts, True
    ),
)


class Reading:
    """State of one optional reading for one pack. Until it first succeeds it is tried on every
    poll, up to TRIES times. After a later failure the last value stays in the state: a missing
    key would make Home Assistant log template errors on every update."""

    def __init__(self, spec: ExtraReading):
        self.spec = spec
        self.value: object = None
        self.fails = 0
        self.stale = False

    @property
    def due(self) -> bool:
        return self.fails < TRIES if self.value is None else self.spec.every_poll


class Pack:
    def __init__(self, config: Config, pack: PackConfig, link: Link):
        self.cfg, self.link = pack, link
        self.topics = ha.topics(config.mqtt, pack)
        self.failures = 0
        self.online: bool | None = None
        self.layout: tuple[int, int] | None = None  # cells, temperature sensors
        self.readings = {spec.name: Reading(spec) for spec in EXTRA_READINGS}
        self.described: tuple | None = None  # what the published discovery was built from

    def __str__(self) -> str:
        return f"{self.cfg.id} ({self.link}/{self.cfg.address})"

    async def request(self, build: Callable[[int], bytes]) -> bytes:
        frame = await self.link.request(build(self.cfg.address), self.cfg.address)
        return frame.info

    async def read(self) -> Analog:
        return decode_analog(await self.request(analog_request))

    @property
    def device(self) -> Device | None:
        return self.readings["device"].value  # type: ignore[return-value]

    @property
    def extras(self) -> dict[str, object]:
        """Optional readings that end up in the state, by name."""
        return {n: r.value for n, r in self.readings.items() if n != "device" and r.value is not None}

    def description(self) -> tuple:
        return (self.layout, self.device, tuple(self.extras))


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        links: dict[tuple, Link] = {}
        self.packs = []
        for p in config.packs:
            if p.bus not in links:
                links[p.bus] = (
                    SerialLink(p.serial, p.baud, config.timeout)
                    if p.serial
                    else TcpLink(p.host, p.port, config.timeout)  # type: ignore[arg-type]
                )
            self.packs.append(Pack(config, p, links[p.bus]))
        self.publish: Publish | None = None

    async def _send(self, topic: str, payload: dict | str, retain: bool = False) -> None:
        assert self.publish is not None
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        await self.publish(topic, payload, retain)

    async def _set_online(self, pack: Pack, online: bool) -> None:
        pack.online = online
        await self._send(pack.topics["availability"], "online" if online else "offline", retain=True)

    async def _discover(self, pack: Pack) -> None:
        assert pack.layout is not None
        pack.described = pack.description()
        payload = ha.discovery(self.config.mqtt, pack.cfg, *pack.layout, pack.device, pack.extras)
        await self._send(pack.topics["discovery"], payload, retain=True)

    async def announce(self) -> None:
        """Publish discovery and availability again (on MQTT connect and Home Assistant restart)."""
        for pack in self.packs:
            if pack.layout is not None:
                await self._discover(pack)
            if pack.online is not None:
                await self._set_online(pack, pack.online)

    async def poll(self, pack: Pack) -> None:
        try:
            analog = await pack.read()
        except (TimeoutError, OSError, EOFError, ProtocolError) as exc:
            pack.failures += 1
            level = logging.WARNING if pack.failures in (1, OFFLINE_AFTER) else logging.DEBUG
            log.log(level, "%s: read failed (%d in a row): %s", pack, pack.failures, str(exc) or "no reply")
            if pack.failures >= OFFLINE_AFTER and pack.online is not False:
                await self._set_online(pack, False)
            return
        if pack.failures >= OFFLINE_AFTER:
            log.info("%s: reading again", pack)
        pack.failures = 0
        layout = (len(analog.cells_mv), len(analog.temp_sensors))
        if layout != pack.layout:
            log.info("%s: %d cells, %d temperature sensors", pack, *layout)
            pack.layout = layout
        for reading in pack.readings.values():
            if reading.due:
                await self._read_optional(pack, reading)
        if pack.description() != pack.described:
            await self._discover(pack)
        if pack.online is not True:
            await self._set_online(pack, True)
        payload = ha.state(analog, pack.extras, self.config.positive_current)
        await self._send(pack.topics["state"], payload)

    async def _read_optional(self, pack: Pack, reading: Reading) -> None:
        what = reading.spec.description
        try:
            value = reading.spec.decode(await pack.request(reading.spec.request))
        except (TimeoutError, OSError, EOFError, ProtocolError) as exc:
            reading.fails += 1
            reason = str(exc) or "no reply"
            if reading.value is None:
                give_up = reading.fails == TRIES
                log.log(
                    logging.WARNING if give_up else logging.DEBUG,
                    "%s: %s unavailable%s: %s",
                    pack,
                    what,
                    ", giving up" if give_up else "",
                    reason,
                )
            elif not reading.stale:
                reading.stale = True
                log.warning("%s: %s not read, repeating the last values: %s", pack, what, reason)
            return
        if reading.value is None:
            # Only a summary at INFO: logs end up in public bug reports.
            summary = _device_summary(value) if isinstance(value, Device) else "read"
            log.info("%s: %s: %s", pack, what, summary)
            log.debug("%s: %s", pack, value)
        elif reading.stale:
            log.info("%s: %s read again", pack, what)
        reading.value, reading.stale = value, False

    async def poll_forever(self, pack: Pack) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await self.poll(pack)
            await asyncio.sleep(max(0.0, self.config.interval - (loop.time() - started)))

    async def close(self) -> None:
        for link in {p.link for p in self.packs}:
            await link.close()


async def run(config: Config) -> None:
    m = config.mqtt
    bridge = Bridge(config)
    bridge_topic = f"{m.base_topic}/bridge/state"
    ha_status = f"{m.discovery_prefix}/status"
    try:
        while True:
            try:
                await _session(bridge, bridge_topic, ha_status)
            except* aiomqtt.MqttError as group:
                log.warning(
                    "MQTT %s:%d: %s; retrying in %d s", m.host, m.port, group.exceptions[0], MQTT_RETRY
                )
            await asyncio.sleep(MQTT_RETRY)
    finally:
        await bridge.close()


async def _session(bridge: Bridge, bridge_topic: str, ha_status: str) -> None:
    m = bridge.config.mqtt
    client = aiomqtt.Client(
        m.host,
        m.port,
        username=m.username,
        password=m.password.get_secret_value() if m.password else None,
        identifier=m.client_id,
        will=aiomqtt.Will(bridge_topic, "offline", qos=1, retain=True),
    )
    async with client:

        async def publish(topic: str, payload: str, retain: bool) -> None:
            await client.publish(topic, payload, qos=1, retain=retain)

        bridge.publish = publish
        log.info("connected to MQTT %s:%d", m.host, m.port)
        await publish(bridge_topic, "online", True)
        await client.subscribe(ha_status)
        await bridge.announce()
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_follow_home_assistant(client, bridge))
                tg.create_task(_heartbeat())
                for pack in bridge.packs:
                    tg.create_task(bridge.poll_forever(pack))
        except asyncio.CancelledError:
            # A clean disconnect does not trigger the will, so say goodbye explicitly.
            with contextlib.suppress(aiomqtt.MqttError, TimeoutError):
                async with asyncio.timeout(2):
                    await publish(bridge_topic, "offline", True)
            raise


async def _follow_home_assistant(client: aiomqtt.Client, bridge: Bridge) -> None:
    async for message in client.messages:
        if message.payload == b"online":
            log.info("Home Assistant started, publishing discovery")
            await bridge.announce()


async def _heartbeat() -> None:
    while True:
        HEALTH_FILE.touch()
        await asyncio.sleep(HEARTBEAT)


def healthy(max_age: float = 3 * HEARTBEAT) -> bool:
    """True while a running bridge is connected to MQTT (used by the container health check)."""
    try:
        return time.time() - HEALTH_FILE.stat().st_mtime < max_age
    except OSError:
        return False
