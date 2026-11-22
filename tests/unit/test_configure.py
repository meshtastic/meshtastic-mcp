# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`configure`: snapshot, apply in one transaction, read back, and measure the restart."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2, mesh_pb2

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
        self.channels = [channel_pb2.Channel(index=i) for i in range(8)]
        self.channels[0].role = channel_pb2.Channel.Role.PRIMARY
        self.channels[0].settings.psk = b"\x01"
        self.uptime = 1000
        self.sent: list[str] = []
        self.drop: set[str] = set()
        self.in_transaction = False
        self.pending_reboot = False

    def store(self, section: str, value) -> None:
        self.sent.append(section)
        if section in self.drop:
            return
        if section == "lora" and value.region == config_pb2.Config.LoRaConfig.RegionCode.EU_868:
            value = type(value)(region=value.region, tx_power=27)  # the firmware caps power
        parent = (
            self.local
            if section in python_configure.protopath.LOCAL_CONFIG_SECTIONS
            else self.module
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
                self.channels = [channel_pb2.Channel() for _ in radio.channels]
                for mine, theirs in zip(self.channels, radio.channels, strict=True):
                    mine.CopyFrom(theirs)

            def writeChannel(self, index):
                radio.sent.append(f"channel{index}")
                radio.channels[index].CopyFrom(self.channels[index])

            def writeConfig(self, section):
                parent = (
                    self.localConfig
                    if section in python_configure.protopath.LOCAL_CONFIG_SECTIONS
                    else self.moduleConfig
                )
                radio.store(section, getattr(parent, section))

            def setOwner(self, long_name=None, short_name=None, is_licensed=False, **_flags):
                radio.sent.append("owner")
                if "owner" not in radio.drop:
                    radio.owner = {"longName": long_name, "shortName": short_name}
                    if is_licensed:
                        radio.owner["isLicensed"] = True

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


def test_a_write_without_a_lease_holds_one_for_the_call(radio, monkeypatch) -> None:
    leases.CURRENT.set(None)
    seen = []
    real_configure = python_configure.configure

    def spy(*args, **kwargs):
        seen.append([r["holder"] for r in leases.active()])
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(python_configure, "configure", spy)
    result = core.configure(node="/dev/ttyUSB0", changes={"lora.region": "EU_868"})
    assert result["verified"] is True
    assert seen == [["configure"]] and leases.active() == []


def test_a_radio_another_session_holds_is_refused(radio) -> None:
    leases.CURRENT.set(None)
    other = leases.claim("/dev/ttyUSB0", holder="someone else")
    try:
        with pytest.raises(ToolError, match="someone else"):
            core.configure(node="/dev/ttyUSB0", changes={"lora.region": "EU_868"})
        assert radio.sent == []
    finally:
        leases.release(other["lease"])


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
    # the power the firmware capped for EU_868 comes back with the region
    assert set(result["changes"]) == {"lora.region", "lora.tx_power", "device.role"}
    assert radio.local.lora.region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert radio.local.security.private_key == b"\x01" * 32
    assert result["verified"] is True


def test_snapshot_and_restore_cover_owner_and_channels(radio) -> None:
    radio.channels[1].role = channel_pb2.Channel.Role.SECONDARY
    radio.channels[1].settings.name = "bench"
    radio.channels[1].settings.psk = b"\x07" * 16
    first = core.configure(changes={"device.role": "CLIENT_MUTE"})
    saved = config_snapshot.load_config(first["snapshot"]["name"])
    assert saved["owner"]["long_name"] == "Heltec"
    assert saved["channels"]["1"]["settings"]["name"] == "bench"
    radio.owner = {"longName": "Erased", "shortName": "E"}
    radio.channels[1].CopyFrom(channel_pb2.Channel(index=1))
    result = core.configure(restore=first["snapshot"]["name"])
    assert radio.owner["longName"] == "Heltec" and radio.owner["shortName"] == "HV3"
    assert radio.channels[1].settings.name == "bench"
    assert radio.channels[1].settings.psk == b"\x07" * 16
    assert result["verified"] is True


def test_diff_withholds_channel_keys(radio) -> None:
    config_snapshot.save("a", "/dev/ttyUSB0", {"channels": {"1": {"settings": {"psk": "AQ=="}}}})
    config_snapshot.save("b", "/dev/ttyUSB0", {"channels": {"1": {"settings": {"psk": "Ag=="}}}})
    changed = config_snapshot.diff("a", "b")["changed"]
    assert changed == {"channels.1.settings.psk": {"from": "<redacted>", "to": "<redacted>"}}


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
            raise MeshInterface.MeshInterfaceError("Timed out waiting for connection completion")
        with real(port=port, timeout_s=timeout_s) as iface:
            yield iface

    monkeypatch.setattr(connection, "connect", flaky)
    result = core.configure(changes={"device.role": "CLIENT_MUTE"}, timeout_s=0.01)
    assert result["verified"] is False and result["restarted"] is None
    assert "did not answer again" in result["errors"]["read_back"]
    assert result["snapshot"]  # the way back survives the failure


def test_a_radio_that_does_not_report_uptime_leaves_restarted_unknown(radio, monkeypatch) -> None:
    monkeypatch.setattr(python_configure, "_uptime", lambda iface, timeout_s=5.0: None)
    result = core.configure(changes={"device.role": "CLIENT_MUTE"})
    assert result["verified"] is True and result["restarted"] is None


def test_a_field_the_firmware_changed_itself_is_a_side_effect_not_a_failure(radio) -> None:
    radio.local.lora.tx_power = 30
    result = core.configure(changes={"lora.region": "EU_868"})
    assert result["verified"] is True and "mismatches" not in result
    assert result["side_effects"] == {"lora.tx_power": {"from": 30, "to": 27}}


def test_a_blank_owner_name_is_refused_before_anything_is_written(radio) -> None:
    with pytest.raises(ToolError, match="cannot be blank"):
        core.configure(changes={"owner.long_name": " "})
    assert radio.sent == []


def test_a_section_the_library_cannot_write_is_refused_before_the_transaction(
    radio, monkeypatch
) -> None:
    monkeypatch.setattr(python_configure, "WRITABLE", frozenset({"lora", "device"}))
    with pytest.raises(ToolError, match="cannot write the mqtt section"):
        core.configure(changes={"lora.region": "EU_868", "mqtt.enabled": True})
    assert radio.sent == []


def test_the_writable_sections_come_from_the_installed_library() -> None:
    assert python_configure.WRITABLE is not None
    assert {"lora", "device", "mqtt"} <= python_configure.WRITABLE


def test_a_library_exit_becomes_a_tool_error_and_prints_nothing(radio, monkeypatch, capsys) -> None:
    from meshtastic.util import our_exit

    def refuse(*args, **kwargs):
        our_exit("Error: No valid config with name lora")

    monkeypatch.setattr(python_configure, "_apply", refuse)
    with pytest.raises(ToolError, match="No valid config with name lora"):
        core.configure(changes={"lora.region": "EU_868"})
    assert capsys.readouterr().out == ""
