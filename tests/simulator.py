"""A fake RS485 gateway with packs behind it, served over TCP for tests."""

import asyncio
import contextlib

from daren2mqtt.protocol import decode


class FakeGateway:
    """Answers each request with ``replies[address]`` (bytes, or a callable returning
    bytes). ``before`` is sent ahead of every reply, as other traffic on the bus."""

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
                reply = self.replies.get(decode(request).adr)
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
