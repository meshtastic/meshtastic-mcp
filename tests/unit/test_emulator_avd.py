# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Portable unit tests for the emulator AVD wrapper (no emulator/hardware needed)."""

from __future__ import annotations

import subprocess

from meshtastic_mcp.emulator import avd


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_tcp_dut_address_uses_host_alias() -> None:
    assert avd.tcp_dut_address(4403) == "10.0.2.2:4403"
    assert avd.tcp_dut_address(4404) == "10.0.2.2:4404"
    assert avd.EMULATOR_HOST_ALIAS == "10.0.2.2"


def test_first_emulator_serial_parses_adb_devices(monkeypatch) -> None:
    out = "List of devices attached\nemulator-5554\tdevice\n127.0.0.1:6555\tdevice\n"
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp(out))
    assert avd.first_emulator_serial() == "emulator-5554"


def test_first_emulator_serial_none_when_no_emulator(monkeypatch) -> None:
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp("List of devices attached\n"))
    assert avd.first_emulator_serial() is None


def test_is_app_installed(monkeypatch) -> None:
    pkgs = "package:com.android.shell\npackage:com.geeksville.mesh\n"
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp(pkgs))
    assert avd.is_app_installed("com.geeksville.mesh") is True
    assert avd.is_app_installed("com.example.absent") is False


def test_ui_dump_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(
        avd,
        "android",
        lambda *a, **k: _cp('[{"text": "Nodes 2/2", "center": "[100,200]"}]'),
    )
    els = avd.ui_dump()
    assert els[0]["text"] == "Nodes 2/2"


def test_find_text(monkeypatch) -> None:
    monkeypatch.setattr(avd, "android", lambda *a, **k: _cp('[{"text": "E2E-123"}]'))
    assert avd.find_text("E2E-123") is True
    assert avd.find_text("nope") is False
