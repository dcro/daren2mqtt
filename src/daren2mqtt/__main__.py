"""Command line: ``run`` the bridge (default), ``read`` one pack, or check ``health``."""

import argparse
import asyncio
import dataclasses
import json
import logging
import signal
import sys

from daren2mqtt import __version__, config, ha
from daren2mqtt.bridge import OPTIONAL, healthy, run
from daren2mqtt.decode import decode_analog
from daren2mqtt.link import SerialLink, TcpLink
from daren2mqtt.protocol import ProtocolError, analog_request

log = logging.getLogger("daren2mqtt")


def parse_target(text: str) -> tuple[str, int | None, int]:
    """``host[:port]/address`` -> (host, port, address); ``/dev/<port>/address`` -> (device, None,
    address) for a local serial port."""
    where, _, address = text.rpartition("/")
    try:
        if where.startswith("/"):
            target: tuple[str, int | None, int] = (where, None, int(address))
        else:
            host, _, port = where.partition(":")
            if not host:
                raise ValueError
            target = (host, int(port or 4196), int(address))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected HOST[:PORT]/ADDRESS or /dev/PORT/ADDRESS, got {text!r}"
        ) from None
    if not 0 <= target[2] <= 15:
        raise argparse.ArgumentTypeError("address must be 0..15")
    return target


async def _read(where: str, port: int | None, address: int, reply_timeout: float, baud: int) -> dict:
    link = SerialLink(where, baud, reply_timeout) if port is None else TcpLink(where, port, reply_timeout)
    result: dict = {}
    try:
        analog = decode_analog((await link.request(analog_request(address), address)).info)
        for spec in OPTIONAL:
            try:
                value = spec.decode((await link.request(spec.request(address), address)).info)
            except (TimeoutError, OSError, EOFError, ProtocolError):
                value = None  # the state is what matters
            result[spec.name] = dataclasses.asdict(value) if value is not None else None
    finally:
        await link.close()
    return result | {"state": ha.state(analog)}


async def _run(cfg: config.Config) -> None:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    await run(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daren2mqtt", description="Daren BMS to MQTT bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the bridge (default)")
    read = sub.add_parser("read", help="read one pack once and print everything it reports as JSON")
    read.add_argument("target", type=parse_target, metavar="HOST[:PORT]/ADDRESS or /dev/PORT/ADDRESS")
    read.add_argument("--baud", type=int, default=9600, help="serial port baud rate (default 9600)")
    read.add_argument("--timeout", type=float, default=2.0, help="reply timeout in seconds (default 2)")
    sub.add_parser("health", help="exit 0 while a running bridge is connected to MQTT")
    args = parser.parse_args(argv)

    if args.command == "health":
        return 0 if healthy() else 1

    if args.command == "read":
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
        try:
            result = asyncio.run(_read(*args.target, args.timeout, args.baud))
        except (TimeoutError, OSError, EOFError, ProtocolError) as exc:
            print(f"error: {str(exc) or 'no reply'}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("daren2mqtt %s, %d pack(s), every %g s", __version__, len(cfg.packs), cfg.interval)
    try:
        asyncio.run(_run(cfg))
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
