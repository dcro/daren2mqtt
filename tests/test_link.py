import asyncio

import pytest
from simulator import FakeGateway
from synthetic import analog_reply

from daren2mqtt.decode import decode_analog
from daren2mqtt.link import Link
from daren2mqtt.protocol import ProtocolError, analog_request, encode


def run(coro):
    return asyncio.run(coro)


async def _read(gateway: FakeGateway, address: int, reply_timeout: float = 0.5):
    link = Link("127.0.0.1", gateway.port, timeout=reply_timeout)
    try:
        return await link.request(analog_request(address), address)
    finally:
        await link.close()


def test_request_and_reply():
    async def go():
        async with FakeGateway({0: analog_reply(0, soc=81.0)}) as gw:
            frame = await _read(gw, 0)
            assert gw.requests == [analog_request(0)]
            return decode_analog(frame.info)

    assert run(go()).soc == pytest.approx(81.0)


def test_echo_other_packs_and_noise_are_skipped():
    other = analog_reply(1, soc=10.0)
    noise = b"\x00\xfe garbage\r~22004A00E0C6BAD\r"

    async def go():
        async with FakeGateway({0: analog_reply(0, soc=55.0)}, before=other + noise, echo=True) as gw:
            return decode_analog((await _read(gw, 0)).info)

    assert run(go()).soc == pytest.approx(55.0)


def test_other_protocol_version_is_skipped_until_timeout():
    pylon = encode(0, 0x00, b"\x01\x02", cid1=0x46, ver=0x20)

    async def go():
        async with FakeGateway({0: pylon}) as gw:
            await _read(gw, 0, reply_timeout=0.3)

    with pytest.raises(TimeoutError):
        run(go())


def test_error_code_raises():
    async def go():
        async with FakeGateway({0: encode(0, 0x90)}) as gw:
            await _read(gw, 0)

    with pytest.raises(ProtocolError, match="address error"):
        run(go())


def test_silent_pack_times_out():
    async def go():
        async with FakeGateway({}) as gw:
            await _read(gw, 3, reply_timeout=0.3)

    with pytest.raises(TimeoutError):
        run(go())


def test_silent_pack_keeps_the_connection():
    async def go():
        async with FakeGateway({0: analog_reply(0)}) as gw:
            link = Link("127.0.0.1", gw.port, timeout=0.2)
            try:
                with pytest.raises(TimeoutError):
                    await link.request(analog_request(5), 5)
                await link.request(analog_request(0), 0)
            finally:
                await link.close()
            return gw.connections

    assert run(go()) == 1


def test_requests_are_serialized_and_connection_reused():
    async def go():
        async with FakeGateway({0: analog_reply(0, soc=10.0), 1: analog_reply(1, soc=20.0)}) as gw:
            link = Link("127.0.0.1", gw.port, timeout=0.5)
            try:
                frames = await asyncio.gather(*(link.request(analog_request(a), a) for a in (0, 1, 0, 1)))
            finally:
                await link.close()
            return [decode_analog(f.info).soc for f in frames], gw.connections

    socs, connections = run(go())
    assert socs == [10.0, 20.0, 10.0, 20.0]
    assert connections == 1


def test_reconnects_after_the_gateway_drops_the_connection():
    async def go():
        async with FakeGateway({0: analog_reply(0)}) as gw:
            link = Link("127.0.0.1", gw.port, timeout=0.5)
            try:
                await link.request(analog_request(0), 0)
                gw.drop()
                await asyncio.sleep(0.05)
                with pytest.raises((EOFError, OSError)):
                    await link.request(analog_request(0), 0)
                await link.request(analog_request(0), 0)
            finally:
                await link.close()
            return gw.connections

    assert run(go()) == 2


def test_unreachable_gateway():
    async def go():
        link = Link("127.0.0.1", 1, timeout=0.5)
        await link.request(analog_request(0), 0)

    with pytest.raises(OSError):
        run(go())
