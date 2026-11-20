# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`send`: one kind of payload, and what the evidence of delivery means."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.protobuf import mesh_pb2, portnums_pb2

from meshtastic_mcp import backends, connection, sdk_cli
from meshtastic_mcp.backends import python_send
from meshtastic_mcp.tools import core

ME = 0x3A14AA13
PEER = 0xC2C91376


class _Radio:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.answer = None  # the packet the mesh sends back, if any

    def _record(self, **kw):
        self.sent.append(kw)
        callback = kw.get("onResponse")
        if callback is not None:
            assert callback.__name__  # meshtastic-python reads it on every reply
            if self.answer is not None:
                callback(self.answer)
        return mesh_pb2.MeshPacket(id=4242)

    def iface(self):
        return SimpleNamespace(
            devPath="/dev/ttyUSB0",
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=ME),
            sendData=lambda data, **kw: self._record(data=data, **kw),
            sendPosition=lambda **kw: self._record(kind="position", **kw),
            sendWaypoint=lambda **kw: self._record(kind="waypoint", **kw),
        )


@pytest.fixture
def radio(monkeypatch):
    r = _Radio()

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        yield r.iface()

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(python_send, "LINGER_S", 0.0)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    return r


def _routing(sender: int, reason: str = "NONE") -> dict:
    return {"from": sender, "decoded": {"routing": {"errorReason": reason}}}


def test_exactly_one_payload(radio) -> None:
    with pytest.raises(ToolError, match="exactly one"):
        core.send(text="hi", request="telemetry", to="!c2c91376")
    with pytest.raises(ToolError, match="exactly one"):
        core.send()


def test_a_broadcast_text_goes_without_waiting(radio) -> None:
    result = core.send(text="hello mesh")
    assert result["destination"] == "^all" and result["packet_id"] == 4242
    assert radio.sent[0]["data"] == b"hello mesh"
    assert radio.sent[0]["portNum"] == portnums_pb2.PortNum.TEXT_MESSAGE_APP
    assert "delivery" not in result


@pytest.mark.parametrize(
    ("answer", "delivery"),
    [
        (_routing(PEER), "delivered"),
        (_routing(ME), "relayed"),
        (_routing(PEER, "MAX_RETRANSMIT"), "nak:MAX_RETRANSMIT"),
        (None, "no_ack"),
    ],
)
def test_a_direct_ack_says_who_answered(radio, answer, delivery) -> None:
    radio.answer = answer
    result = core.send(text="hi", to="!c2c91376", want_ack=True, wait_s=0.01)
    assert result["delivery"] == delivery
    assert radio.sent[0]["onResponseAckPermitted"] is True


def test_want_ack_on_a_broadcast_is_refused(radio) -> None:
    with pytest.raises(ToolError, match="direct"):
        core.send(text="hi", want_ack=True)


def test_a_traceroute_returns_the_route(radio) -> None:
    route = {"route": [123], "snrTowards": [24]}
    radio.answer = {"from": PEER, "decoded": {"traceroute": {**route, "raw": object()}}}
    result = core.send(request="traceroute", to=PEER)
    assert result["reply"] == route and result["kind"] == "traceroute"
    assert radio.sent[0]["portNum"] == portnums_pb2.PortNum.TRACEROUTE_APP
    assert radio.sent[0]["destinationId"] == PEER


def test_a_telemetry_request_returns_the_metrics(radio) -> None:
    metrics = {"deviceMetrics": {"uptimeSeconds": 99}}
    radio.answer = {"from": PEER, "decoded": {"telemetry": metrics}}
    assert core.send(request="telemetry", to="!c2c91376")["reply"] == metrics


def test_a_request_nobody_answers_says_so(radio) -> None:
    assert core.send(request="telemetry", to="!c2c91376", wait_s=0.01)["reply"] is None


def test_a_request_needs_a_destination(radio) -> None:
    with pytest.raises(ToolError, match="one node"):
        core.send(request="traceroute")


def test_a_position_carries_its_coordinates(radio) -> None:
    core.send(position={"latitude": 47.6, "longitude": -122.3, "altitude": 50})
    sent = radio.sent[0]
    assert sent["kind"] == "position" and sent["latitude"] == 47.6 and sent["altitude"] == 50


def test_ble_text_goes_through_the_sdk(radio, monkeypatch) -> None:
    monkeypatch.setattr(sdk_cli, "available", lambda: True)
    calls = []

    def send_text(target, message, *, to, channel, await_ms):
        calls.append((target, message, to))
        return {
            "ok": True,
            "error": None,
            "by_type": {
                "info": [
                    {"kind": "send-queued", "messageId": "77"},
                    {"kind": "send-result", "outcome": "success"},
                ]
            },
        }

    monkeypatch.setattr(sdk_cli, "send_text", send_text)
    result = core.send(node="ble:Heltec", to="!c2c91376", text="hi")
    assert calls == [("ble:Heltec", "hi", str(PEER))]
    assert result["delivery"] == "delivered" and result["packet_id"] == "77"


def test_an_sdk_timeout_is_no_ack(radio, monkeypatch) -> None:
    monkeypatch.setattr(sdk_cli, "available", lambda: True)
    monkeypatch.setattr(
        sdk_cli,
        "send_text",
        lambda *a, **k: {"ok": False, "error": {"code": "timeout"}, "by_type": {}},
    )
    assert core.send(node="ble:Heltec", text="hi")["delivery"] == "no_ack"
