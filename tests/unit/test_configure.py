# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`configure`: snapshot, apply in one transaction, read back, and measure the restart."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.protobuf import config_pb2, localonly_pb2, mesh_pb2

from meshtastic_mcp import backends, config_snapshot, connection, leases
from meshtastic_mcp.backends import python_configure
from meshtastic_mcp.tools import core

NUM = 0x3A14AA13


class _Radio:
    """The device side: what is stored, what was sent, and how long it has been up."""

    def __init__(self) -> None:
        self.local = localonly_pb2.LocalConfig()
        self.local.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        self.local.device.SetInParent()
        self.local.security.private_key = b"\x01" * 32
        self.module = localonly_pb2.LocalModuleConfig()
        self.module.mqtt.SetInParent()
        self.owner = {"longName": "Heltec", "shortName": "HV3"}
        self.uptime = 1000
        self.sent: list[str] = []
        self.drop: set[str] = set()
        self.in_transaction = False
        self.pending_reboot = False

    def store(self, section: str, value) -> None:
        self.sent.append(section)
        if section in self.drop:
            return
        parent = (
            self.local if section in python_configure.admin.LOCAL_CONFIG_SECTIONS else self.module
        )
        getattr(parent, section).CopyFrom(value)
        if section != "lora" and not self.in_transaction:
            self.pending_reboot = True

    def answer_telemetry(self, request, *, destinationId, portNum, wantResponse, onResponse):
        assert destinationId == NUM and request.HasField("device_metrics")
        onResponse({"decoded": {"telemetry": {"deviceMetrics": {"uptimeSeconds": self.uptime}}}})

    def connect(self):
        if self.pending_reboot:
            self.uptime = 1
            self.pending_reboot = False
        radio = self

        class Node:
            def __init__(self) -> None:
                self.localConfig = localonly_pb2.LocalConfig()
                self.localConfig.CopyFrom(radio.local)
                self.moduleConfig = localonly_pb2.LocalModuleConfig()
                self.moduleConfig.CopyFrom(radio.module)

            def writeConfig(self, section):
                parent = (
                    self.localConfig
                    if section in python_configure.admin.LOCAL_CONFIG_SECTIONS
                    else self.moduleConfig
                )
                radio.store(section, getattr(parent, section))

            def setOwner(self, long_name=None, short_name=None):
                radio.sent.append("owner")
                if "owner" not in radio.drop:
                    radio.owner = {"longName": long_name, "shortName": short_name}

            def beginSettingsTransaction(self):
                radio.sent.append("begin")
                radio.in_transaction = True

            def commitSettingsTransaction(self):
                radio.sent.append("commit")
                radio.in_transaction = False
                radio.pending_reboot = True  # a commit always restarts

        return SimpleNamespace(
            devPath="/dev/ttyUSB0",
            localNode=Node(),
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=NUM),
            sendData=self.answer_telemetry,
            nodesByNum={NUM: {"num": NUM, "user": dict(self.owner)}},
        )


@pytest.fixture
def radio(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    monkeypatch.setattr(python_configure.time, "sleep", lambda s: None)
    r = _Radio()
    r.connects = 0

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        r.connects += 1
        yield r.connect()

    monkeypatch.setattr(connection, "connect", fake_connect)
    token = leases.CURRENT.set("lease-1")
    yield r
    leases.CURRENT.reset(token)


def test_a_write_needs_a_lease(radio) -> None:
    leases.CURRENT.set(None)
    with pytest.raises(ToolError, match="claim it first"):
        core.configure(changes={"lora.region": "EU_868"})
    assert radio.sent == []


def test_dry_run_reports_and_writes_nothing(radio) -> None:
    leases.CURRENT.set(None)
    result = core.configure(changes={"lora.region": "EU_868"}, dry_run=True)
    assert result["changes"] == {"lora.region": {"from": "US", "to": "EU_868"}}
    assert radio.sent == [] and "snapshot" not in result


def test_one_live_section_goes_without_a_transaction(radio) -> None:
    result = core.configure(changes={"lora.region": "EU_868"})
    assert radio.sent == ["lora"]
    assert result["verified"] is True and result["restarted"] is False
    assert result["transaction"] is False
    assert radio.connects == 2  # the write, then a fresh read-back


def test_several_changes_share_one_paced_transaction(radio) -> None:
    result = core.configure(
        changes={"lora.region": "EU_868", "device.role": "CLIENT_MUTE", "owner.long_name": "Base"}
    )
    assert radio.sent[0] == "begin" and radio.sent[-1] == "commit"
    assert set(radio.sent[1:-1]) == {"lora", "device", "owner"}
    assert result["transaction"] is True and result["restarted"] is True
    assert result["uptime_s"] == {"before": 1000, "after": 1}
    assert result["changes"]["owner.long_name"] == {"from": "Heltec", "to": "Base"}
    assert result["verified"] is True


def test_a_dropped_write_is_caught_on_read_back(radio) -> None:
    radio.drop = {"device"}
    result = core.configure(changes={"device.role": "CLIENT_MUTE"})
    assert result["verified"] is False
    assert result["mismatches"] == {"device.role": {"wanted": "CLIENT_MUTE", "read_back": "CLIENT"}}


def test_a_value_already_set_writes_nothing(radio) -> None:
    result = core.configure(changes={"lora.region": "US"})
    assert result["changes"] == {} and radio.sent == [] and result["verified"] is True


def test_a_secret_change_is_seen_and_shown_withheld(radio) -> None:
    result = core.configure(changes={"mqtt.password": "hunter2"})
    assert radio.sent == ["mqtt"]
    assert result["changes"] == {"mqtt.password": {"from": "", "to": "<redacted>"}}


def test_restore_puts_the_snapshot_back_but_never_the_keys(radio) -> None:
    first = core.configure(changes={"lora.region": "EU_868", "device.role": "CLIENT_MUTE"})
    snapshot = first["snapshot"]["name"]
    saved = config_snapshot.load_config(snapshot)
    saved["localConfig"]["security"]["private_key"] = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="
    config_snapshot.save(snapshot, "/dev/ttyUSB0", saved)
    radio.sent.clear()
    result = core.configure(restore=snapshot)
    assert set(result["changes"]) == {"lora.region", "device.role"}
    assert radio.local.lora.region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert radio.local.security.private_key == b"\x01" * 32
    assert result["verified"] is True


def test_a_bad_value_is_refused_before_anything_is_written(radio) -> None:
    with pytest.raises(ToolError, match="not a valid"):
        core.configure(changes={"lora.region": "MARS"})
    assert radio.sent == []


def test_a_radio_that_never_comes_back_is_reported(radio, monkeypatch) -> None:
    real = connection.connect
    calls = {"n": 0}

    @contextmanager
    def flaky(port=None, timeout_s=8.0, linger_s=0.0):
        calls["n"] += 1
        if calls["n"] > 1:
            raise connection.ConnectionError("Serial open failed")
        with real(port=port, timeout_s=timeout_s) as iface:
            yield iface

    monkeypatch.setattr(connection, "connect", flaky)
    result = core.configure(changes={"device.role": "CLIENT_MUTE"}, timeout_s=0.01)
    assert result["verified"] is False and result["restarted"] is None
    assert "did not answer again" in result["errors"]["read_back"]


def test_a_radio_that_does_not_report_uptime_leaves_restarted_unknown(radio, monkeypatch) -> None:
    monkeypatch.setattr(python_configure, "_uptime", lambda iface, timeout_s=5.0: None)
    result = core.configure(changes={"device.role": "CLIENT_MUTE"})
    assert result["verified"] is True and result["restarted"] is None
