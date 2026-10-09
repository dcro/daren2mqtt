"""End to end against a real MQTT broker, with a simulated gateway.

Runs when DAREN2MQTT_TEST_BROKER=host:port points at a throwaway broker (CI starts one).
Topics get a random prefix and retained messages are cleared at the end.
"""

import asyncio
import contextlib
import os
import uuid

import aiomqtt
import pytest
from simulator import FakeGateway
from synthetic import pack_replies

from daren2mqtt import bridge
from daren2mqtt.config import Config

BROKER = os.environ.get("DAREN2MQTT_TEST_BROKER")
pytestmark = pytest.mark.skipif(not BROKER, reason="set DAREN2MQTT_TEST_BROKER=host:port")


def test_bridge_against_a_broker(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "HEALTH_FILE", tmp_path / "health")
    host, _, port = (BROKER or "").partition(":")
    run = uuid.uuid4().hex[:8]
    base, prefix = f"d2m-{run}", f"ha-{run}"
    discovery = f"{prefix}/device/daren2mqtt_a/config"
    retained = [discovery, f"{base}/a/availability", f"{base}/bridge/state"]

    async def go():
        async with (
            FakeGateway({1: pack_replies(1, soc=55.0)}) as gw,
            aiomqtt.Client(host, int(port or 1883), identifier=f"observer-{run}") as observer,
        ):
            config = Config.model_validate(
                {
                    "timeout": 0.5,
                    "mqtt": {
                        "host": host,
                        "port": int(port or 1883),
                        "client_id": f"daren2mqtt-{run}",
                        "base_topic": base,
                        "discovery_prefix": prefix,
                    },
                    "packs": [{"id": "a", "host": "127.0.0.1", "port": gw.port, "address": 1}],
                }
            )
            await observer.subscribe(f"{base}/#")
            await observer.subscribe(f"{prefix}/#")
            seen: list[tuple[str, str]] = []

            async def until(topic: str, payload: str | None = None) -> None:
                async with asyncio.timeout(10):
                    async for message in observer.messages:
                        seen.append((str(message.topic), message.payload.decode()))
                        if seen[-1][0] == topic and payload in (None, seen[-1][1]):
                            return

            task = asyncio.create_task(bridge.run(config))
            try:
                await until(f"{base}/a")  # the first state
                topics = [t for t, _ in seen]
                assert (f"{base}/bridge/state", "online") in seen
                assert (f"{base}/a/availability", "online") in seen
                assert topics.index(discovery) < topics.index(f"{base}/a")

                await observer.publish(f"{prefix}/status", "online")  # Home Assistant restarted
                await until(discovery)
                assert [t for t, _ in seen].count(discovery) == 2
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await until(f"{base}/bridge/state", "offline")  # a clean stop says goodbye
            for topic in retained:
                await observer.publish(topic, b"", retain=True)

    asyncio.run(go())
