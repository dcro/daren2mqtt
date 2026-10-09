"""Polls every pack and publishes its state, availability and discovery over MQTT."""

import asyncio
import contextlib
import json
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiomqtt

from daren2mqtt import ha
from daren2mqtt.config import Config, PackConfig
from daren2mqtt.decode import Analog, Counters, Device, decode_analog, decode_counters, decode_device
from daren2mqtt.link import Link
from daren2mqtt.protocol import ProtocolError, analog_request, counters_request, device_request

log = logging.getLogger(__name__)

OFFLINE_AFTER = 3  # consecutive failed reads before a pack is reported unavailable
TRIES = 3  # attempts at an optional reading (device information, counters) before doing without it
MQTT_RETRY = 10  # seconds
HEARTBEAT = 15  # seconds between touches of the health file while connected to MQTT
HEALTH_FILE = Path(tempfile.gettempdir()) / "daren2mqtt.health"

Publish = Callable[[str, str, bool], Awaitable[None]]


class Pack:
    def __init__(self, config: Config, pack: PackConfig, link: Link):
        self.cfg, self.link = pack, link
        self.topics = ha.topics(config.mqtt, pack)
        self.failures = 0
        self.online: bool | None = None
        self.layout: tuple[int, int] | None = None  # cells, temperature sensors
        self.device: Device | None = None
        self.device_tries = 0
        self.counters: Counters | None = None
        self.counters_fails = 0
        self.described: tuple | None = None  # what the published discovery was built from

    def __str__(self) -> str:
        return f"{self.cfg.id} ({self.link}/{self.cfg.address})"

    async def read(self) -> Analog:
        frame = await self.link.request(analog_request(self.cfg.address), self.cfg.address)
        return decode_analog(frame.info)

    async def read_device(self) -> Device:
        frame = await self.link.request(device_request(self.cfg.address), self.cfg.address)
        return decode_device(frame.info)

    async def read_counters(self) -> Counters:
        frame = await self.link.request(counters_request(self.cfg.address), self.cfg.address)
        return decode_counters(frame.info)


class Bridge:
    def __init__(self, config: Config, links: dict[tuple[str, int], Link] | None = None):
        self.config = config
        links = {} if links is None else links
        self.packs = []
        for p in config.packs:
            link = links.setdefault((p.host, p.port), Link(p.host, p.port, config.timeout))
            self.packs.append(Pack(config, p, link))
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
        pack.described = (pack.layout, pack.device, pack.counters is not None)
        payload = ha.discovery(
            self.config.mqtt, pack.cfg, *pack.layout, pack.device, pack.counters is not None
        )
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
        if pack.device is None and pack.device_tries < TRIES:
            await self._read_device(pack)
        if pack.counters is not None or pack.counters_fails < TRIES:
            await self._read_counters(pack)
        if (pack.layout, pack.device, pack.counters is not None) != pack.described:
            await self._discover(pack)
        if pack.online is not True:
            await self._set_online(pack, True)
        await self._send(pack.topics["state"], ha.state(analog, pack.counters))

    async def _read_device(self, pack: Pack) -> None:
        pack.device_tries += 1
        try:
            pack.device = await pack.read_device()
        except (TimeoutError, OSError, EOFError, ProtocolError) as exc:
            last = pack.device_tries == TRIES
            log.log(
                logging.WARNING if last else logging.DEBUG,
                "%s: device information unavailable%s: %s",
                pack,
                ", giving up" if last else "",
                str(exc) or "no reply",
            )
            return
        d = pack.device
        log.info(
            "%s: model %s, hardware %s, firmware %s", pack, d.model or "?", d.hardware or "?", d.firmware
        )

    async def _read_counters(self, pack: Pack) -> None:
        try:
            pack.counters = await pack.read_counters()
        except (TimeoutError, OSError, EOFError, ProtocolError) as exc:
            # After a failure the last values stay in the state: a missing key would make
            # Home Assistant log template errors on every update.
            pack.counters_fails += 1
            give_up = pack.counters is None and pack.counters_fails == TRIES
            log.log(
                logging.WARNING if give_up else logging.DEBUG,
                "%s: counters unavailable%s: %s",
                pack,
                ", giving up" if give_up else "",
                str(exc) or "no reply",
            )

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
