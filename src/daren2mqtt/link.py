"""TCP connection to an RS485-to-Ethernet gateway (raw TCP server mode)."""

import asyncio
import contextlib
import logging

from daren2mqtt.protocol import CID1_BMS, RETURN_CODES, VER, Frame, ProtocolError, decode

log = logging.getLogger(__name__)


class Link:
    """One persistent connection per gateway. RS485 is half duplex, so requests are
    sent one at a time. Gateways forward every byte on the bus to every TCP client,
    so a reply is only accepted if it comes from the address that was asked."""

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
        self._reader = self._writer = None

    async def request(self, frame: bytes, address: int) -> Frame:
        async with self._lock:
            try:
                return await self._exchange(frame, address)
            except TimeoutError:
                raise  # a silent pack is not a broken connection
            except (OSError, EOFError, asyncio.LimitOverrunError):
                await self.close()  # reconnect on the next request
                raise

    async def _exchange(self, frame: bytes, address: int) -> Frame:
        if self._writer is None or self._writer.is_closing():
            async with asyncio.timeout(self.timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            log.info("connected to %s", self)
        assert self._reader is not None
        self._writer.write(frame)
        await self._writer.drain()
        async with asyncio.timeout(self.timeout):
            while True:
                try:
                    raw = await self._reader.readuntil(b"\r")
                except asyncio.IncompleteReadError as exc:
                    raise EOFError(f"{self} closed the connection") from exc
                start = raw.rfind(b"~")
                if start < 0 or raw[start:] == frame:
                    continue  # line noise, or our own request echoed back
                try:
                    reply = decode(raw[start:])
                except ProtocolError as exc:
                    log.debug("%s: discarding frame: %s", self, exc)
                    continue
                if reply.ver != VER or reply.cid1 != CID1_BMS or reply.adr != address:
                    continue  # another pack, another client's exchange, or another protocol
                if reply.cid2 != 0:
                    reason = RETURN_CODES.get(reply.cid2, "unknown")
                    raise ProtocolError(f"address {address} returned error {reply.cid2:02X} ({reason})")
                return reply
