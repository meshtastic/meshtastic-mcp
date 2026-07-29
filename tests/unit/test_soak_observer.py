# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SoakObserver: pubsub record synthesis, telemetry attribution, keep-alive
relay contract, and reconnect bookkeeping — with faked interfaces (no serial
hardware, no meshtastic library connects)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services import soak_observer
from meshtastic_mcp.web.services.soak_observer import SoakObserver, _Held


class FakeIface:
    def __init__(self, dev_path: str) -> None:
        self.devPath = dev_path
        self.sent_texts: list[tuple[str, str, int]] = []


class FakeHub:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish_threadsafe(self, topic: str, data: dict) -> None:
        self.published.append((topic, data))


def _observer(**kwargs) -> tuple[SoakObserver, list[dict], list[dict]]:
    records: list[dict] = []
    telem: list[dict] = []
    obs = SoakObserver(on_record=records.append, on_telemetry=telem.append, hub=FakeHub(), **kwargs)
    return obs, records, telem


def _hold(obs: SoakObserver, serial: str, port: str) -> _Held:
    held = _Held(serial=serial, port=port, iface=FakeIface(port))
    obs._held[serial] = held
    obs._by_port[port] = held
    return held


def test_receive_text_record_carries_message_text():
    obs, records, _ = _observer()
    _hold(obs, "S1", "/dev/a")
    obs._on_receive(
        {
            "from": 0xAABBCCDD,
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "nightly-15-3"},
        },
        interface=FakeIface("/dev/a"),
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "packet" and rec["serial"] == "S1"
    # The message text is in the line, so the traffic-loss grep can find it.
    assert "nightly-15-3" in rec["line"]
    assert rec["text"] == "nightly-15-3"
    assert rec["from_node"] == 0xAABBCCDD
    # Teed to the device's serial.* WS topic for the UI.
    assert obs.hub.published and obs.hub.published[0][0] == "serial.S1"


def test_receive_other_and_encrypted_portnums():
    obs, records, _ = _observer()
    _hold(obs, "S1", "/dev/a")
    obs._on_receive(
        {"from": 1, "decoded": {"portnum": "TELEMETRY_APP"}}, interface=FakeIface("/dev/a")
    )
    obs._on_receive({"from": 2}, interface=FakeIface("/dev/a"))  # no decoded => encrypted
    assert "RX TELEMETRY_APP" in records[0]["line"]
    assert "RX ENCRYPTED" in records[1]["line"]


def test_unknown_interface_is_ignored():
    obs, records, _ = _observer()
    _hold(obs, "S1", "/dev/a")
    obs._on_receive(
        {"from": 1, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "x"}},
        interface=FakeIface("/dev/other"),
    )
    obs._on_log_line("boot", interface=None)
    assert records == []


def test_log_line_record_parsed():
    obs, records, _ = _observer()
    _hold(obs, "S1", "/dev/a")
    obs._on_log_line(
        "INFO  | 12:00:00 77 [Power] Battery: batPct=88", interface=FakeIface("/dev/a")
    )
    assert records and records[0]["kind"] == "log"
    assert records[0]["serial"] == "S1"
    assert "Battery" in records[0]["line"]


def test_telemetry_attributed_to_originating_node():
    obs, _, telem = _observer(node_map={0x11: "PEER", 0x22: "SELF"})
    _hold(obs, "SELF", "/dev/a")
    obs._on_telemetry_packet(
        {
            "from": 0x11,
            "decoded": {
                "telemetry": {"deviceMetrics": {"batteryLevel": 91, "channelUtilization": 4.5}}
            },
        },
        interface=FakeIface("/dev/a"),
    )
    kinds = {(t["serial"], t["kind"], t["value"]) for t in telem}
    # Attributed to the node the metrics are ABOUT (the sender), not the observer.
    assert ("PEER", "battery", 91) in kinds
    assert ("PEER", "channel_utilization", 4.5) in kinds


def test_telemetry_unknown_sender_falls_back_to_observer_serial():
    obs, _, telem = _observer(node_map={})
    _hold(obs, "S1", "/dev/a")
    obs._on_telemetry_packet(
        {"from": 0x99, "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 12}}}},
        interface=FakeIface("/dev/a"),
    )
    assert telem and telem[0]["serial"] == "S1" and telem[0]["from_node"] == 0x99


def test_connection_lost_emits_status_once_and_marks_lost():
    obs, records, _ = _observer()
    held = _hold(obs, "S1", "/dev/a")
    obs._on_connection_lost(interface=FakeIface("/dev/a"))
    obs._on_connection_lost(interface=FakeIface("/dev/a"))  # duplicate event
    assert held.lost is True
    status = [r for r in records if r["kind"] == "status"]
    assert len(status) == 1 and "connection lost" in status[0]["line"]


def test_send_text_requires_live_session():
    obs, _, _ = _observer()
    held = _hold(obs, "S1", "/dev/a")

    def fake_send(text, destinationId, channelIndex):
        held.iface.sent_texts.append((text, destinationId, channelIndex))

        class P:
            id = 42

        return P()

    held.iface.sendText = fake_send

    async def go():
        assert await obs.send_text("S1", "hello") == 42
        held.lost = True
        with pytest.raises(Exception, match="no live soak observer session"):
            await obs.send_text("S1", "hello2")
        with pytest.raises(Exception, match="no live soak observer session"):
            await obs.send_text("NOPE", "hello3")

    asyncio.run(go())
    assert held.iface.sent_texts == [("hello", "^all", 0)]


def test_send_input_event_relay_contract():
    obs, _, _ = _observer()
    held = _hold(obs, "S1", "/dev/a")

    async def go():
        # Not ours -> False (caller falls back to its own path).
        assert await obs.send_input_event("OTHER", "USER_PRESS") is False
        # Ours but down -> True (swallowed; do not fight the port).
        held.lost = True
        assert await obs.send_input_event("S1", "USER_PRESS") is True

    asyncio.run(go())


def test_health_tick_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(soak_observer, "RECONNECT_SPACING_S", 0.0)
    obs, records, _ = _observer()
    held = _hold(obs, "S1", "/dev/a")
    held.lost = True

    def failing_open(h):
        raise RuntimeError("port gone")

    monkeypatch.setattr(obs, "_open_blocking", failing_open)
    monkeypatch.setattr(obs, "_close_blocking", lambda h: None)

    async def go():
        for _ in range(soak_observer.RECONNECT_MAX_ATTEMPTS + 2):
            await obs.health_tick()

    asyncio.run(go())
    assert held.gave_up is True
    lines = [r["line"] for r in records]
    # Note: the "giving up" line also contains the words "reconnect attempts" —
    # count only the per-attempt status lines by their prefix.
    attempts = sum(ln.startswith("— reconnect attempt") for ln in lines)
    assert attempts == soak_observer.RECONNECT_MAX_ATTEMPTS
    assert any("giving up" in ln for ln in lines)

    # close_all reports it as dropped.
    async def close():
        await obs.close_all()

    asyncio.run(close())
    assert obs.summary.dropped == ["S1"]


def test_health_tick_reconnect_success_resets_state(monkeypatch):
    monkeypatch.setattr(soak_observer, "RECONNECT_SPACING_S", 0.0)
    obs, records, _ = _observer()
    held = _hold(obs, "S1", "/dev/a")
    held.lost = True
    monkeypatch.setattr(obs, "_close_blocking", lambda h: None)
    monkeypatch.setattr(obs, "_open_blocking", lambda h: None)

    async def go():
        await obs.health_tick()

    asyncio.run(go())
    assert held.lost is False and held.reconnect_attempts == 0 and not held.gave_up
    assert any("reconnected" in r["line"] for r in records)
