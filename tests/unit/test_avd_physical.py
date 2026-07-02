# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Physical-device branching + uiautomator XML parsing in emulator/avd.py.

These are pure functions (no subprocess) except the reverse-tunnel side effect,
which is monkeypatched. They guard the physical-Android paths the e2e helpers
rely on — chiefly the negative-bounds regression (partially off-screen views).
"""

from __future__ import annotations

import pytest

from meshtastic_mcp.emulator import avd


def test_is_emulator():
    assert avd.is_emulator("emulator-5554")
    assert not avd.is_emulator("R5CT80ABCDE")  # a physical-device serial


def test_parse_uiautomator_negative_bounds_keeps_center():
    # Partially off-screen view reports a negative left/top — must still yield a
    # tappable center (regression: a bare \\d+ regex dropped this element).
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<hierarchy>"
        '<node text="Skip" clickable="true" bounds="[-5,84][1080,210]"/>'
        "</hierarchy>"
    )
    els = avd._parse_uiautomator_xml(xml)
    skip = next(e for e in els if e["text"] == "Skip")
    assert skip["center"] == [(-5 + 1080) // 2, (84 + 210) // 2]
    assert "clickable" in skip["interactions"]


def test_parse_uiautomator_positive_bounds_and_interactions():
    xml = (
        "<hierarchy>"
        '<node text="Add" focusable="true" scrollable="true" bounds="[0,0][100,40]"/>'
        "</hierarchy>"
    )
    els = avd._parse_uiautomator_xml(xml)
    add = next(e for e in els if e["text"] == "Add")
    assert add["center"] == [50, 20]
    assert "focusable" in add["interactions"]
    assert "scrollable" in add["interactions"]


def test_parse_uiautomator_malformed_raises():
    # A truncated/partial dump must be distinguishable from an empty screen.
    with pytest.raises(avd.EmulatorError):
        avd._parse_uiautomator_xml("<hierarchy><node bounds=")


def test_ui_dump_physical_strips_trailing_status_line(monkeypatch):
    # Regression (found live 2026-07-01 against a Pixel 6a / Android 17): some
    # uiautomator versions append "UI hierchary dumped to: /dev/tty" *after*
    # </hierarchy> on the same line with no separating newline, which used to
    # blow up ET.fromstring with "junk after document element" and break every
    # UI-tree-dependent helper on physical devices.
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        '<node text="Disconnect" clickable="true" bounds="[0,0][200,60]"/>'
        "</hierarchy>UI hierchary dumped to: /dev/tty\n"
    )
    monkeypatch.setattr(avd, "adb", lambda *a, **k: _cp_stdout(raw))
    els = avd._ui_dump_physical("R5CT80ABCDE")
    assert any(e["text"] == "Disconnect" for e in els)


def _cp_stdout(stdout: str):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_tcp_dut_address_emulator_uses_host_alias():
    assert avd.tcp_dut_address(4403, serial="emulator-5554") == "10.0.2.2:4403"
    assert avd.tcp_dut_address(4403) == "10.0.2.2:4403"  # no serial → emulator default


def test_tcp_dut_address_physical_sets_reverse_tunnel(monkeypatch):
    calls = []
    monkeypatch.setattr(
        avd,
        "adb_reverse",
        lambda host_port, device_port, serial=None: calls.append((host_port, device_port, serial)),
    )
    addr = avd.tcp_dut_address(4403, serial="R5CT80ABCDE")
    assert addr == "127.0.0.1:4403"
    assert calls == [(4403, 4403, "R5CT80ABCDE")]
