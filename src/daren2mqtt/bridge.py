"""Polls every pack and publishes its state, availability and discovery over MQTT."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

from daren2mqtt import ha
from daren2mqtt.config import Config, PackConfig
from daren2mqtt.decode import Analog, decode_analog
from daren2mqtt.link import Link
from daren2mqtt.protocol import ProtocolError, analog_request

log = logging.getLogger(__name__)

OFFLINE_AFTER = 3  # consecutive failed reads before a pack is reported unavailable
MQTT_RETRY = 10  # seconds

Publish = Callable[[str, str, bool], Awaitable[None]]


class Pack:
    def __init__(self, config: Config, pack: PackConfig, link: Link):
        self.cfg, self.link = pack, link
        self.topics = ha.topics(config.mqtt, pack)
        self.failures = 0
        self.online: bool | None = None
        self.layout: tuple[int, int] | None = None  # cells, temperature sensors

    def __str__(self) -> str:
        return f"{self.cfg.id} ({self.link}/{self.cfg.address})"

    async def read(self) -> Analog:
        frame = await self.link.request(analog_request(self.cfg.address), self.cfg.address)
        return decode_analog(frame.info)


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
        payload = ha.discovery(self.config.mqtt, pack.cfg, *pack.layout)
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
            log.log(
                level, "%s: read failed (%d in a row): %s", pack, pack.failures, exc or type(exc).__name__
            )
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
            await self._discover(pack)
        if pack.online is not True:
            await self._set_online(pack, True)
        await self._send(pack.topics["state"], ha.state(analog))

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
        password=m.password,
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
