# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Requests, remote admin and the non-section configure keys, through faked radios."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.protobuf import channel_pb2, localonly_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

from meshtastic_mcp import backends, connection
from meshtastic_mcp.backends import python_admin, python_configure, python_send
from meshtastic_mcp.backends.python import PythonBackend
from meshtastic_mcp.tools import core

ME = 0x3A14AA13
PEER = 0xC2C91376


class _Node:
    """Records the library calls a backend makes, with the library's own signatures."""

    def __init__(self, hop_limit: int = 5) -> None:
        self.calls: list[tuple] = []
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.hop_limit = hop_limit
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels = [channel_pb2.Channel(index=i) for i in range(8)]
        self.channels[0].role = channel_pb2.Channel.Role.PRIMARY
        self.channels[1].role = channel_pb2.Channel.Role.SECONDARY
        self.channels[1].settings.name = "ops"
        self.nodeNum = ME

    def setTime(self, timeSec: int = 0):
        self.calls.append(("setTime", timeSec))

    def addContactURL(self, url: str):
        self.calls.append(("addContactURL", url))

    def enterDFUMode(self):
        self.calls.append(("enterDFUMode",))

    def setOwner(self, long_name=None, short_name=None, is_licensed=False, is_unmessagable=None):
        self.calls.append(("setOwner", long_name, short_name, is_licensed, is_unmessagable))

    def writeChannel(self, channelIndex, adminIndex=0):
        self.calls.append(("writeChannel", channelIndex))

    def deleteChannel(self, channelIndex):
        self.calls.append(("deleteChannel", channelIndex))

    def setFixedPosition(self, lat, lon, alt):
        self.calls.append(("setFixedPosition", lat, lon, alt))

    def removeFixedPosition(self):
        self.calls.append(("removeFixedPosition",))

    def set_canned_message(self, message):
        self.calls.append(("set_canned_message", message))

    def set_ringtone(self, ringtone):
        self.calls.append(("set_ringtone", ringtone))

    def setURL(self, url: str, addOnly: bool = False):
        self.calls.append(("setURL", url, addOnly))

    def getContactURL(self, node_id, should_ignore=False, manually_verified=False):
        return f"https://meshtastic.org/v/#{node_id:08x}"

    def beginSettingsTransaction(self):
        self.calls.append(("begin",))

    def commitSettingsTransaction(self):
        self.calls.append(("commit",))


class _Radio:
    def __init__(self) -> None:
        self.node = _Node()
        self.sent: list[dict] = []
        self.answer = None

    def _record(self, data, **kw):
        self.sent.append({"data": data, **kw})
        if kw.get("onResponse") is not None and self.answer is not None:
            kw["onResponse"](self.answer)
        return mesh_pb2.MeshPacket(id=7)

    def iface(self):
        user = {"longName": "Base", "shortName": "BASE", "isLicensed": False}
        return SimpleNamespace(
            devPath="/dev/ttyUSB0",
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=ME),
            localNode=self.node,
            nodesByNum={ME: {"user": user}},
            nodes={},
            sendData=self._record,
        )


@pytest.fixture
def radio(monkeypatch):
    r = _Radio()

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        yield r.iface()

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(python_send, "LINGER_S", 0.0)
    monkeypatch.setattr(python_admin.time, "sleep", lambda s: None)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    return r


@pytest.mark.parametrize(
    ("variant", "field"),
    [
        ("environment", "environment_metrics"),
        ("local_stats", "local_stats"),
        ("host", "host_metrics"),
    ],
)
def test_a_telemetry_request_names_its_variant(radio, variant, field) -> None:
    core.send(request="telemetry", telemetry=variant, to=PEER, wait_s=0.01)
    sent = radio.sent[0]
    assert isinstance(sent["data"], telemetry_pb2.Telemetry)
    assert sent["data"].WhichOneof("variant") == field  # firmware ignores a request naming none


def test_requests_carry_want_ack_and_the_radios_hop_limit(radio) -> None:
    for request in ("traceroute", "position", "neighbor_info", "node_info", "telemetry"):
        core.send(request=request, to=PEER, wait_s=0.01)
    assert [s["wantAck"] for s in radio.sent] == [True] * 5
    assert {s["hopLimit"] for s in radio.sent} == {5}  # lora.hop_limit, not a fixed 7
    ports = [s["portNum"] for s in radio.sent]
    assert ports == [
        portnums_pb2.PortNum.TRACEROUTE_APP,
        portnums_pb2.PortNum.POSITION_APP,
        portnums_pb2.PortNum.NEIGHBORINFO_APP,
        portnums_pb2.PortNum.NODEINFO_APP,
        portnums_pb2.PortNum.TELEMETRY_APP,
    ]
    assert radio.sent[3]["data"].long_name == "Base"  # a node_info request carries ours


def test_a_caller_hop_limit_wins(radio) -> None:
    core.send(request="traceroute", to=PEER, hop_limit=2, wait_s=0.01)
    assert radio.sent[0]["hopLimit"] == 2


def test_a_position_reply_is_returned(radio) -> None:
    radio.answer = {"from": PEER, "decoded": {"position": {"latitudeI": 1}}}
    assert core.send(request="position", to=PEER)["reply"] == {"latitudeI": 1}


def test_set_time_and_add_contact(radio) -> None:
    out = asyncio.run(core.admin(None, action="set_time", time_s=1_800_000_000))
    assert out["time"] == 1_800_000_000
    asyncio.run(core.admin(None, action="add_contact", contact_url="https://meshtastic.org/v/#abc"))
    with pytest.raises(ToolError, match="contact URL"):
        asyncio.run(core.admin(None, action="add_contact", contact_url="nope"))
    assert ("setTime", 1_800_000_000) in radio.node.calls
    assert ("addContactURL", "https://meshtastic.org/v/#abc") in radio.node.calls


def test_a_remote_refusal_names_the_admin_key(radio, monkeypatch) -> None:
    acks = python_admin._Acks()

    def fake_remote(iface, dest, timeout_s):
        node = _Node()
        node.reboot = lambda secs=10: acks.on_ack_nak(
            {"decoded": {"routing": {"errorReason": "ADMIN_PUBLIC_KEY_UNAUTHORIZED"}}}
        )
        return node, acks

    monkeypatch.setattr(python_admin, "remote_node", fake_remote)
    iface_extra = SimpleNamespace(reset=lambda: None)
    monkeypatch.setattr(
        _Radio,
        "iface",
        lambda self: SimpleNamespace(
            devPath="/dev/ttyUSB0",
            localNode=self.node,
            _acknowledgment=iface_extra,
            waitForAckNak=lambda: None,
        ),
    )
    with pytest.raises(backends.BackendError, match="admin_key"):
        PythonBackend().admin(None, "reboot", None, 3, False, 10.0, dest="!c2c91376")


def test_a_remote_ack_reports_the_dest(radio, monkeypatch) -> None:
    acks = python_admin._Acks()

    def fake_remote(iface, dest, timeout_s):
        node = _Node()
        node.setTime = lambda timeSec=0: acks.on_ack_nak(
            {"from": PEER, "decoded": {"routing": {"errorReason": "NONE"}}}
        )
        return node, acks

    monkeypatch.setattr(python_admin, "remote_node", fake_remote)
    monkeypatch.setattr(
        _Radio,
        "iface",
        lambda self: SimpleNamespace(
            devPath="/dev/ttyUSB0",
            localNode=self.node,
            _acknowledgment=SimpleNamespace(reset=lambda: None),
            waitForAckNak=lambda: None,
        ),
    )
    out = PythonBackend().admin(None, "set_time", None, 3, False, 10.0, dest="!c2c91376")
    assert out["dest"] == "!c2c91376" and out["ok"]


def test_enter_uf2_never_goes_remote(radio) -> None:
    with pytest.raises(backends.BackendError, match="USB"):
        PythonBackend().admin(None, "enter_uf2", None, 0, False, 10.0, dest="!c2c91376")


def _plan(radio, changes):
    iface = radio.iface()
    plan = python_configure._Plan()
    python_configure._plan_changes(plan, iface.localNode, changes)
    python_configure._finish_plan(plan, iface)
    return plan, iface


def test_owner_flags_go_through_set_owner(radio) -> None:
    plan, iface = _plan(radio, {"owner.is_licensed": True, "owner.is_unmessagable": "true"})
    assert plan.changes["owner.is_licensed"] == {"from": False, "to": True}
    python_configure._apply(plan, iface)
    assert ("setOwner", "Base", "BASE", True, True) in radio.node.calls


def test_channel_edit_add_and_delete(radio) -> None:
    plan, iface = _plan(
        radio,
        {"channel.2.name": "new", "channel.2.psk": "random", "channel.1.delete": True},
    )
    assert plan.changes["channel.1"]["to"] == "deleted"
    assert plan.changes["channel.2"]["to"]["role"] == "SECONDARY"  # setting a field adds it
    python_configure._apply(plan, iface)
    calls = [c for c in radio.node.calls if c[0] in ("writeChannel", "deleteChannel")]
    assert calls == [("writeChannel", 2), ("deleteChannel", 1)]
    assert radio.node.channels[2].settings.name == "new"
    assert len(radio.node.channels[2].settings.psk) == 32


def test_the_primary_channel_is_never_deleted(radio) -> None:
    with pytest.raises(backends.BackendError, match="primary"):
        _plan(radio, {"channel.0.delete": True})


def test_fixed_position_canned_messages_and_ringtone(radio) -> None:
    plan, iface = _plan(
        radio,
        {
            "fixed_position": {"latitude": 30.1, "longitude": -97.7},
            "canned_messages": ["yes", "no"],
            "ringtone": "beep:d=4,o=5,b=120:c",
        },
    )
    python_configure._apply(plan, iface)
    assert ("setFixedPosition", 30.1, -97.7, 0) in radio.node.calls
    assert ("set_canned_message", "yes|no") in radio.node.calls
    assert ("set_ringtone", "beep:d=4,o=5,b=120:c") in radio.node.calls
    plan, iface = _plan(radio, {"fixed_position": None})
    python_configure._apply(plan, iface)
    assert ("removeFixedPosition",) in radio.node.calls


def test_set_channel_url_add_only_uses_the_add_form(radio) -> None:
    out = core.set_channel_url(url="https://meshtastic.org/e/#Cg0", add_only=True)
    call = next(c for c in radio.node.calls if c[0] == "setURL")
    assert call == ("setURL", "https://meshtastic.org/e/?add=true#Cg0", True)
    assert "channels_added" in out


def test_set_channel_url_refuses_a_non_url(radio) -> None:
    with pytest.raises(ToolError, match="not a channel URL"):
        core.set_channel_url(url="LongFast")


def test_inspect_reads_the_contact_url(radio) -> None:
    out = core.inspect(what=["contact_url"])
    assert out["contact_url"] == f"https://meshtastic.org/v/#{ME:08x}"


def test_input_events_are_range_checked_before_any_connection(radio) -> None:
    with pytest.raises(ToolError, match="kb_char"):
        core.send_input_event(event_code="RIGHT", kb_char=300)
