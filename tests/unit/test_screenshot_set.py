# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Screen resolution and plan validation for screenshot_set (no emulator or simulator)."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshtastic_mcp import screenshot_set as ss


def test_shared_screen_maps_each_platform() -> None:
    assert ss.resolve_screen("nodes") == ("nodes", {"android": "nodes", "ios": "nodes"})
    assert ss.resolve_screen("connections")[1] == {"android": "connections", "ios": "connect"}
    assert ss.resolve_screen("settings/lora") == (
        "settings-lora",
        {"android": "settings/lora", "ios": "settings/lora"},
    )


def test_node_and_channel_use_each_apps_own_link_shape() -> None:
    assert ss.resolve_screen("node:1234")[1] == {
        "android": "nodes/1234",
        "ios": "nodes?nodenum=1234",
    }
    assert ss.resolve_screen("channel:0")[1] == {
        "android": "messages/0^all",
        "ios": "messages?channelId=0",
    }


def test_custom_screen_takes_explicit_paths() -> None:
    name, paths = ss.resolve_screen({"name": "discovery", "android": "/discovery"})
    assert name == "discovery"
    assert paths == {"android": "discovery"}


@pytest.mark.parametrize("spec", ["nope", "node:abc", {"name": "x"}, {"android": "nodes"}])
def test_unknown_screens_are_rejected(spec) -> None:
    with pytest.raises(ValueError):
        ss.resolve_screen(spec)


def test_make_plan_validates_platforms_and_appearances(tmp_path: Path) -> None:
    plan = ss.make_plan(["nodes", "map"], ["android", "ios"], ["dark"], tmp_path)
    assert [n for n, _ in plan.screens] == ["nodes", "map"]
    with pytest.raises(ValueError):
        ss.make_plan(["nodes"], ["web"], ["light"], tmp_path)
    with pytest.raises(ValueError):
        ss.make_plan(["nodes"], ["ios"], ["sepia"], tmp_path)
    with pytest.raises(ValueError):
        ss.make_plan([], ["ios"], ["light"], tmp_path)


def test_android_finds_a_dialog_button_in_a_second_window(monkeypatch) -> None:
    # `android layout` returns one entry per window; a dialog's buttons live in the
    # second window's content, and "Connect to this device?" must not match "Connect".
    import subprocess

    from meshtastic_mcp.emulator import avd

    layout = (
        '[{"window-title": "", "content": [{"text": "Nodes", "center": "[1,1]"}]},'
        ' {"window-title": "", "content": ['
        '{"text": "Connect to this device?", "center": "[540,989]"},'
        '{"text": "Connect", "center": "[754,1431]"}]}]'
    )
    monkeypatch.setattr(
        avd,
        "android",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=layout, stderr=""),
    )
    monkeypatch.setattr(avd, "first_emulator_serial", lambda: "emulator-5554")
    driver = ss.AndroidDriver(None)
    assert driver._center_of("Connect") == (754, 1431)
    assert driver._center_of("Cancel") is None
