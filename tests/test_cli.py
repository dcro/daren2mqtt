import argparse
import asyncio
import json
import threading

import pytest
from simulator import FakeGateway
from synthetic import analog_reply, counters_reply, device_reply

from daren2mqtt.__main__ import main, parse_target


@pytest.mark.parametrize(
    ("text", "target"),
    [("192.0.2.1/0", ("192.0.2.1", 4196, 0)), ("gw.lan:5000/15", ("gw.lan", 5000, 15))],
)
def test_parse_target(text, target):
    assert parse_target(text) == target


@pytest.mark.parametrize("text", ["192.0.2.1", "/1", "host/16", "host:x/1", "host/a"])
def test_parse_target_rejects(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target(text)


def test_read_prints_state(capsys):
    ready, done = threading.Event(), threading.Event()
    port = []

    def serve():
        async def go():
            replies = {2: {0x42: analog_reply(2, soc=42.0), 0x51: device_reply(2), 0xB0: counters_reply(2)}}
            async with FakeGateway(replies) as gw:
                port.append(gw.port)
                ready.set()
                await asyncio.to_thread(done.wait, 5)

        asyncio.run(go())

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        ready.wait(5)
        assert main(["read", f"127.0.0.1:{port[0]}/2"]) == 0
    finally:
        done.set()
        thread.join()
    out = json.loads(capsys.readouterr().out)
    assert out["state"]["soc"] == 42.0 and out["device"]["firmware"] == "01.02.03"
    assert out["counters"]["charged_kwh"] == 63.2


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
