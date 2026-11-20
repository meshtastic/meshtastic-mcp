# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`inspect`: one connection, defaults reported as values, secrets withheld."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.protobuf import (
    admin_pb2,
    channel_pb2,
    connection_status_pb2,
    localonly_pb2,
    mesh_pb2,
)

from meshtastic_mcp import backends, connection, sdk_cli
from meshtastic_mcp.backends import python as py_backend
from meshtastic_mcp.tools import core

NUM = 0x3A14AA13


class _Node:
    def __init__(self, status: connection_status_pb2.DeviceConnectionStatus | None) -> None:
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels: list[channel_pb2.Channel] = []
        self._status = status
        self.admin_requests: list[admin_pb2.AdminMessage] = []

    def getURL(self, includeAll: bool = True) -> str:
        return "https://meshtastic.org/e/#all" if includeAll else "https://meshtastic.org/e/#one"

    def _sendAdmin(self, msg, wantResponse=True, onResponse=None):
        self.admin_requests.append(msg)
        if self._status is None:
            return  # the radio never answers
        reply = admin_pb2.AdminMessage(get_device_connection_status_response=self._status)
        onResponse({"decoded": {"admin": {"raw": reply}}})


def _radio(status=None):
    node = _Node(status)
    node.localConfig.lora.SetInParent()  # sent, every field at its default
    node.localConfig.security.private_key = b"\x01" * 32
    node.localConfig.security.SetInParent()
    node.moduleConfig.mqtt.password = "hunter2"
    node.moduleConfig.mqtt.enabled = True
    primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
    primary.settings.psk = b"\x01"
    primary.settings.name = "bench"
    node.channels = [primary, channel_pb2.Channel(index=1)]
    return SimpleNamespace(
        devPath="/dev/ttyUSB0",
        localNode=node,
        myInfo=mesh_pb2.MyNodeInfo(my_node_num=NUM),
        metadata=mesh_pb2.DeviceMetadata(firmware_version="2.8.3"),
        nodesByNum={
            NUM: {"num": NUM, "user": {"longName": "Heltec", "shortName": "HV3"}, "raw": object()},
            7: {"num": 7, "user": {"longName": "peer"}, "hopsAway": 2, "snr": 4.5},
        },
    )


@pytest.fixture
def radio(monkeypatch):
    connects: list[str | None] = []
    holder = {"iface": _radio()}

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        connects.append(port)
        yield holder["iface"]

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    return SimpleNamespace(connects=connects, holder=holder)


def test_every_part_comes_over_one_connection(radio) -> None:
    status = connection_status_pb2.DeviceConnectionStatus()
    status.serial.is_connected = True
    radio.holder["iface"] = _radio(status)
    result = core.inspect(what="all")
    assert radio.connects == [None]
    assert {"summary", "config", "channels", "nodes", "network"} <= set(result)
    assert "url" not in result  # the channel keys come only when asked for
    assert result["backend"] == "python" and result["target"] == "/dev/ttyUSB0"
    assert "errors" not in result


def test_summary_names_the_radio_and_its_enums(radio) -> None:
    summary = core.inspect()["summary"]
    assert summary["node_id"] == "!3a14aa13" and summary["long_name"] == "Heltec"
    assert summary["region"] == "UNSET" and summary["firmware_version"] == "2.8.3"
    assert summary["num_nodes"] == 2 and summary["primary_channel"] == "bench"


def test_defaults_are_values_and_an_unsent_section_is_null(radio) -> None:
    config = core.inspect(what="config")["config"]
    assert config["lora"]["region"] == "UNSET"
    assert config["lora"]["hop_limit"] == 0
    assert config["power"] is None  # the radio never sent it
    assert set(config) == set(py_backend.SECTIONS)


def test_a_section_name_reads_that_section_alone(radio) -> None:
    result = core.inspect(what=["lora", "config.mqtt"])
    assert set(result["config"]) == {"lora", "mqtt"}
    assert "summary" not in result


def test_secrets_are_withheld_but_their_absence_shows(radio) -> None:
    result = core.inspect(what=["security", "mqtt", "channels"])
    assert result["config"]["security"]["private_key"] == "<redacted>"
    assert result["config"]["mqtt"]["password"] == "<redacted>"
    assert result["config"]["mqtt"]["enabled"] is True
    assert result["channels"][0]["settings"]["psk"] == "<redacted>"
    assert [c["index"] for c in result["channels"]] == [0]  # the disabled slot is left out


def test_an_empty_secret_stays_empty(radio) -> None:
    radio.holder["iface"].localNode.moduleConfig.mqtt.password = ""
    assert core.inspect(what="mqtt")["config"]["mqtt"]["password"] == ""


def test_nodes_brief_and_full(radio) -> None:
    brief = core.inspect(what="nodes")["nodes"]
    peer = next(n for n in brief if n["node_num"] == 7)
    assert peer["node_id"] == "!00000007" and peer["hops_away"] == 2 and peer["snr"] == 4.5
    full = core.inspect(what="nodes", detail="full")["nodes"]
    assert all("raw" not in n for n in full)


def test_network_decodes_the_address_and_hides_the_pin(radio) -> None:
    status = connection_status_pb2.DeviceConnectionStatus()
    status.wifi.ssid = "bench"
    status.wifi.status.is_connected = True
    status.wifi.status.ip_address = 0x0501A8C0  # 192.168.1.5, network order in a LE fixed32
    status.bluetooth.pin = 123456
    radio.holder["iface"] = _radio(status)
    network = core.inspect(what="network")["network"]
    assert network["wifi"]["status"]["ip_address"] == "192.168.1.5"
    assert network["wifi"]["ssid"] == "bench"
    assert network["bluetooth"]["pin"] == "<redacted>"
    assert network["ethernet"] is None and network["serial"] is None
    request = radio.holder["iface"].localNode.admin_requests[0]
    assert request.get_device_connection_status_request


def test_a_failed_part_is_reported_beside_the_rest(radio, monkeypatch) -> None:
    monkeypatch.setattr(py_backend.threading.Event, "wait", lambda self, timeout=None: False)
    result = core.inspect(what=["summary", "network"])
    assert result["summary"]["long_name"] == "Heltec"
    assert "no connection status" in result["errors"]["network"]


def test_when_every_part_fails_the_call_fails(radio, monkeypatch) -> None:
    monkeypatch.setattr(py_backend.threading.Event, "wait", lambda self, timeout=None: False)
    with pytest.raises(ToolError, match="network"):
        core.inspect(what="network")


def test_an_unknown_part_is_refused(radio) -> None:
    with pytest.raises(ToolError, match="config section"):
        core.inspect(what="everything")


def test_a_ble_target_goes_to_the_sdk(radio, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(sdk_cli, "available", lambda: True)

    def device_info(transport, *, timeout_ms):
        calls.append(transport)
        own = {"num": NUM, "user": {"long_name": "Heltec", "hw_model": "HELTEC_V3"}}
        return {"ok": True, "info": {"ownNode": own, "nodeCount": 16}}

    monkeypatch.setattr(sdk_cli, "device_info", device_info)
    result = core.inspect(node="ble:Heltec")
    assert calls == ["ble:Heltec"] and radio.connects == []
    assert result["backend"] == "sdk"
    assert result["summary"]["node_id"] == "!3a14aa13" and result["summary"]["num_nodes"] == 16


def test_the_sdk_backend_names_what_it_cannot_read(radio, monkeypatch) -> None:
    monkeypatch.setattr(sdk_cli, "available", lambda: True)
    with pytest.raises(ToolError, match="reads only"):
        core.inspect(node="ble:Heltec", what="config")


def test_a_ble_target_without_the_sdk_says_how_to_get_it(radio, monkeypatch) -> None:
    monkeypatch.setattr(sdk_cli, "available", lambda: False)
    with pytest.raises(ToolError, match=sdk_cli.CLI_ENV):
        core.inspect(node="ble:Heltec")


def test_the_channel_url_comes_when_named(radio) -> None:
    url = core.inspect(what="url")["url"]
    assert url == {
        "primary": "https://meshtastic.org/e/#one",
        "all": "https://meshtastic.org/e/#all",
    }
