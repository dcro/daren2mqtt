"""Fake RS485 buses with packs on them, over TCP (a gateway) or a pseudo terminal (a serial port)."""

import asyncio
import contextlib
import os

from daren2mqtt.protocol import CID2_ANALOG, decode


class FakeGateway:
    """Answers each request with ``replies[address]``: a dict keyed by command (CID2) of bytes
    or callables returning bytes, or just bytes / a callable for the analog request (42H).
    ``before`` is sent ahead of every reply, as other traffic on the bus. A request without a
    reply gets silence."""

    def __init__(self, replies: dict, before: bytes = b"", echo: bool = False, split: int = 7):
        self.replies, self.before, self.echo, self.split = replies, before, echo, split
        self.requests: list[bytes] = []
        self.connections = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    def drop(self) -> None:
        """Close every client connection, as a gateway reboot would."""
        for writer in self._writers:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        self._writers.add(writer)
        try:
            while True:
                request = await reader.readuntil(b"\r")
                self.requests.append(request)
                frame = decode(request)
                reply = self.replies.get(frame.adr)
                if not isinstance(reply, dict):
                    reply = {CID2_ANALOG: reply}
                reply = reply.get(frame.cid2)
                if callable(reply):
                    reply = reply()
                out = (request if self.echo else b"") + self.before + (reply or b"")
                for i in range(0, len(out), self.split):  # arrive in chunks, like serial reads
                    writer.write(out[i : i + self.split])
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


def _answer(replies: dict, request: bytes) -> bytes:
    frame = decode(request)
    reply = replies.get(frame.adr)
    if not isinstance(reply, dict):
        reply = {CID2_ANALOG: reply}
    reply = reply.get(frame.cid2)
    return (reply() if callable(reply) else reply) or b""


class FakeSerialPort:
    """A pseudo terminal standing in for a USB-RS485 adapter: ``device`` is opened like
    /dev/ttyUSB0, and the packs in ``replies`` answer on the other side."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.requests: list[bytes] = []
        self._buffer = b""

    async def __aenter__(self):
        self._master, self._slave = os.openpty()
        self.device = os.ttyname(self._slave)
        asyncio.get_running_loop().add_reader(self._master, self._on_data)
        return self

    async def __aexit__(self, *exc):
        asyncio.get_running_loop().remove_reader(self._master)
        os.close(self._master)
        os.close(self._slave)

    def _on_data(self):
        self._buffer += os.read(self._master, 1024)
        while b"\r" in self._buffer:
            request, _, self._buffer = self._buffer.partition(b"\r")
            request += b"\r"
            self.requests.append(request)
            os.write(self._master, _answer(self.replies, request))
