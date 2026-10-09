import asyncio
import json

import pytest
from simulator import FakeSerialPort
from synthetic import analog_reply, pack_replies

from daren2mqtt.bridge import Bridge
from daren2mqtt.config import Config
from daren2mqtt.decode import decode_analog
from daren2mqtt.link import SerialLink
from daren2mqtt.protocol import analog_request


def test_request_and_reply_over_a_serial_port():
    async def go():
        async with FakeSerialPort({1: analog_reply(1, soc=61.0)}) as port:
            link = SerialLink(port.device, 9600, timeout=1)
            try:
                frame = await link.request(analog_request(1), 1)
            finally:
                await link.close()
            return decode_analog(frame.info), port.requests

    analog, requests = asyncio.run(go())
    assert analog.soc == pytest.approx(61.0) and requests == [analog_request(1)]


def test_silent_pack_on_a_serial_port_times_out():
    async def go():
        async with FakeSerialPort({}) as port:
            link = SerialLink(port.device, 9600, timeout=0.2)
            try:
                await link.request(analog_request(3), 3)
            finally:
                await link.close()

    with pytest.raises(TimeoutError):
        asyncio.run(go())


def test_missing_serial_port():
    async def go():
        await SerialLink("/dev/does-not-exist", 9600, timeout=0.5).request(analog_request(0), 0)

    with pytest.raises(OSError):
        asyncio.run(go())


def test_bridge_with_packs_on_one_serial_port():
    async def go():
        async with FakeSerialPort({0: pack_replies(0, soc=50.0), 1: pack_replies(1, soc=60.0)}) as port:
            config = Config.model_validate(
                {
                    "timeout": 0.5,
                    "packs": [
                        {"id": "a", "serial": port.device, "address": 0},
                        {"id": "b", "serial": port.device, "address": 1},
                    ],
                }
            )
            bridge = Bridge(config)
            sent = []

            async def publish(topic, payload, retain):
                sent.append((topic, payload))

            bridge.publish = publish
            assert bridge.packs[0].link is bridge.packs[1].link
            for pack in bridge.packs:
                await bridge.poll(pack)
            await bridge.close()
            return sent

    states = {t: json.loads(p)["soc"] for t, p in asyncio.run(go()) if t in ("daren2mqtt/a", "daren2mqtt/b")}
    assert states == {"daren2mqtt/a": 50.0, "daren2mqtt/b": 60.0}
