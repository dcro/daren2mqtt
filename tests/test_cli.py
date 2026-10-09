import argparse
import asyncio
import contextlib
import json
import threading

import pytest
from simulator import FakeGateway
from synthetic import pack_replies

from daren2mqtt.__main__ import main, parse_target
from daren2mqtt.link import Link


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("192.0.2.1/0", ("192.0.2.1", 4196, 0)),
        ("gw.lan:5000/15", ("gw.lan", 5000, 15)),
        ("/dev/ttyUSB0/1", ("/dev/ttyUSB0", None, 1)),
    ],
)
def test_parse_target(text, target):
    assert parse_target(text) == target


@pytest.mark.parametrize("text", ["192.0.2.1", "/1", "host/16", "host:x/1", "host/a", "/dev/ttyUSB0/x"])
def test_parse_target_rejects(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target(text)


@contextlib.contextmanager
def gateway_in_thread(replies):
    """A FakeGateway on its own event loop, so ``main()`` can call ``asyncio.run`` itself."""
    ready, done = threading.Event(), threading.Event()
    port = []

    def serve():
        async def go():
            async with FakeGateway(replies) as gw:
                port.append(gw.port)
                ready.set()
                await asyncio.to_thread(done.wait, 5)

        asyncio.run(go())

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        ready.wait(5)
        yield port[0]
    finally:
        done.set()
        thread.join()


def test_read_prints_state(capsys):
    with gateway_in_thread({2: pack_replies(2, soc=42.0)}) as port:
        assert main(["read", f"127.0.0.1:{port}/2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"]["soc"] == 42.0 and out["device"]["firmware"] == "01.02.03"
    assert out["counters"]["charged_kwh"] == 63.2


def test_read_survives_a_dropped_connection_after_the_state(monkeypatch, capsys):
    original = Link.request

    async def flaky(self, request, address):
        if request[7:9] != b"42":
            raise ConnectionResetError("gateway went away")
        return await original(self, request, address)

    monkeypatch.setattr(Link, "request", flaky)
    with gateway_in_thread({2: pack_replies(2, soc=42.0)}) as port:
        assert main(["read", f"127.0.0.1:{port}/2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"]["soc"] == 42.0 and out["device"] is None and out["counters"] is None


def test_read_reports_errors(capsys):
    assert main(["read", "127.0.0.1:1/0", "--timeout", "0.5"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_missing_configuration_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DAREN2MQTT_CONFIG", raising=False)
    monkeypatch.setenv("DAREN2MQTT_CONFIG_FILE", str(tmp_path / "none.yaml"))
    assert main([]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_health_without_a_running_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr("daren2mqtt.bridge.HEALTH_FILE", tmp_path / "health")
    assert main(["health"]) == 1
    (tmp_path / "health").touch()
    assert main(["health"]) == 0


@pytest.mark.parametrize("baud", ["300", "fast", "1000000"])
def test_read_rejects_bad_baud_rates(baud, capsys):
    with pytest.raises(SystemExit):
        main(["read", "/dev/ttyUSB0/0", "--baud", baud])
    assert "baud rate must be" in capsys.readouterr().err
