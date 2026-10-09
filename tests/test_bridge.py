import asyncio
import json

from simulator import FakeGateway
from synthetic import analog_reply, device_reply

from daren2mqtt.bridge import DEVICE_TRIES, OFFLINE_AFTER, Bridge
from daren2mqtt.config import Config


def make_bridge(port: int) -> tuple[Bridge, list]:
    config = Config.model_validate(
        {"timeout": 0.2, "packs": [{"id": "a", "host": "127.0.0.1", "port": port, "address": 1}]}
    )
    bridge = Bridge(config)
    sent: list[tuple[str, str, bool]] = []

    async def publish(topic: str, payload: str, retain: bool) -> None:
        sent.append((topic, payload, retain))

    bridge.publish = publish
    return bridge, sent


def topics(sent):
    return [
        (topic, payload if not payload.startswith("{") else "{...}", retain)
        for topic, payload, retain in sent
    ]


def test_first_read_publishes_discovery_availability_and_state():
    async def go():
        async with FakeGateway({1: analog_reply(1, soc=64.0)}) as gw:
            bridge, sent = make_bridge(gw.port)
            await bridge.poll(bridge.packs[0])
            await bridge.poll(bridge.packs[0])
            await bridge.close()
            return sent

    sent = asyncio.run(go())
    assert topics(sent) == [
        ("homeassistant/device/daren2mqtt_a/config", "{...}", True),
        ("daren2mqtt/a/availability", "online", True),
        ("daren2mqtt/a", "{...}", False),
        ("daren2mqtt/a", "{...}", False),
    ]
    assert json.loads(sent[2][1])["soc"] == 64.0


def test_unavailable_after_consecutive_failures_and_back():
    replies: dict = {}

    async def go():
        async with FakeGateway(replies) as gw:
            bridge, sent = make_bridge(gw.port)
            pack = bridge.packs[0]
            for _ in range(OFFLINE_AFTER + 1):
                await bridge.poll(pack)
            offline = list(sent)
            replies[1] = analog_reply(1)
            await bridge.poll(pack)
            await bridge.close()
            return offline, sent[len(offline) :]

    offline, after = asyncio.run(go())
    assert offline == [("daren2mqtt/a/availability", "offline", True)]
    assert [t for t, _, _ in after] == [
        "homeassistant/device/daren2mqtt_a/config",
        "daren2mqtt/a/availability",
        "daren2mqtt/a",
    ]
    assert after[1][1] == "online"


def test_announce_republishes_known_packs_only():
    async def go():
        async with FakeGateway({1: analog_reply(1)}) as gw:
            bridge, sent = make_bridge(gw.port)
            await bridge.announce()
            assert sent == []
            await bridge.poll(bridge.packs[0])
            sent.clear()
            await bridge.announce()
            await bridge.close()
            return sent

    assert [t for t, _, _ in asyncio.run(go())] == [
        "homeassistant/device/daren2mqtt_a/config",
        "daren2mqtt/a/availability",
    ]


def test_packs_on_the_same_gateway_share_a_link():
    config = Config.model_validate(
        {
            "packs": [
                {"id": "a", "host": "192.0.2.1"},
                {"id": "b", "host": "192.0.2.1", "address": 1},
                {"id": "c", "host": "192.0.2.2"},
            ]
        }
    )
    links = [p.link for p in Bridge(config).packs]
    assert links[0] is links[1] and links[0] is not links[2]


def test_device_information_in_discovery():
    async def go():
        async with FakeGateway({1: {0x42: analog_reply(1), 0x51: device_reply(1, model="16S100A")}}) as gw:
            bridge, sent = make_bridge(gw.port)
            await bridge.poll(bridge.packs[0])
            await bridge.poll(bridge.packs[0])
            await bridge.close()
            return sent, gw.requests

    sent, requests = asyncio.run(go())
    discoveries = [json.loads(p) for t, p, _ in sent if t.endswith("/config")]
    assert len(discoveries) == 1 and discoveries[0]["dev"]["model"] == "16S100A"
    assert [r[7:9] for r in requests] == [b"42", b"51", b"42"]  # read once, not on every poll


def test_device_information_is_optional():
    replies: dict = {1: {0x42: analog_reply(1)}}

    async def go():
        async with FakeGateway(replies) as gw:
            bridge, sent = make_bridge(gw.port)
            pack = bridge.packs[0]
            await bridge.poll(pack)
            first = [json.loads(p) for t, p, _ in sent if t.endswith("/config")]
            replies[1][0x51] = device_reply(1, model="16S100A")
            await bridge.poll(pack)  # device information arrives later: discovery again
            for _ in range(DEVICE_TRIES):
                await bridge.poll(pack)
            await bridge.close()
            return first, sent, gw.requests

    first, sent, requests = asyncio.run(go())
    assert first[0]["dev"]["model"] == "16S BMS"
    discoveries = [json.loads(p) for t, p, _ in sent if t.endswith("/config")]
    assert [d["dev"]["model"] for d in discoveries] == ["16S BMS", "16S100A"]
    assert sum(r[7:9] == b"51" for r in requests) == 2


def test_device_information_gives_up():
    async def go():
        async with FakeGateway({1: {0x42: analog_reply(1)}}) as gw:
            bridge, sent = make_bridge(gw.port)
            for _ in range(DEVICE_TRIES + 2):
                await bridge.poll(bridge.packs[0])
            await bridge.close()
            return gw.requests

    requests = asyncio.run(go())
    assert sum(r[7:9] == b"51" for r in requests) == DEVICE_TRIES
