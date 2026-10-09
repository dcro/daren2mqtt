"""Connection to the RS485 bus: through an Ethernet gateway (raw TCP server mode) or a local
serial port (a USB-RS485 adapter)."""

import asyncio
import contextlib
import logging

import serial_asyncio_fast

from daren2mqtt.protocol import CID1_BMS, RETURN_CODES, VER, Frame, ProtocolError, decode

log = logging.getLogger(__name__)


class Link:
    """One persistent connection to an RS485 bus. RS485 is half duplex, so requests are sent
    one at a time. Gateways forward every byte on the bus to every TCP client, so a reply is
    only accepted if it comes from the address that was asked."""

    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise NotImplementedError

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
                self._reader, self._writer = await self._open()
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


class TcpLink(Link):
    """An RS485-to-Ethernet gateway in raw TCP server mode."""

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        super().__init__(timeout)
        self.host, self.port = host, port

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(self.host, self.port)


class SerialLink(Link):
    """A local serial port, 8N1, such as a USB-RS485 adapter. Experimental: not yet tried on a
    battery."""

    def __init__(self, device: str, baud: int = 9600, timeout: float = 2.0):
        super().__init__(timeout)
        self.device, self.baud = device, baud

    def __str__(self) -> str:
        return self.device

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await serial_asyncio_fast.open_serial_connection(url=self.device, baudrate=self.baud)
