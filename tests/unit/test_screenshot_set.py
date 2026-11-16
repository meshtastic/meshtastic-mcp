# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Screen resolution and plan validation for screenshot_set (no emulator or simulator)."""

from __future__ import annotations

import threading
import time
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


def test_make_plan_rejects_screens_with_no_path_for_the_platforms(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ss.make_plan([{"name": "map", "ios": "map"}], ["android"], ["light"], tmp_path)


def test_runs_wait_for_each_other(monkeypatch, tmp_path: Path) -> None:
    active: list[int] = []
    overlap: list[bool] = []

    def fake_run(plan, state, log_path) -> None:
        active.append(1)
        overlap.append(len(active) > 1)
        time.sleep(0.2)
        active.pop()

    monkeypatch.setattr(ss, "_run", fake_run)
    plan = ss.make_plan(["nodes"], ["android"], ["light"], tmp_path)
    threads = [
        threading.Thread(target=ss.run, args=(plan, {}, tmp_path / f"{i}.log")) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert overlap == [False, False]


def test_android_reset_refuses_a_release_build(monkeypatch) -> None:
    # A release build ignores the onboarding skip, so clearing it would strand it on the intro.
    from meshtastic_mcp.emulator import avd

    shell: list[tuple[str, ...]] = []
    monkeypatch.setattr(avd, "first_emulator_serial", lambda: "emulator-5554")
    monkeypatch.setattr(avd, "resolve_package", lambda serial=None: "com.geeksville.mesh")
    monkeypatch.setattr(avd, "adb", lambda *a, **k: shell.append(a))
    driver = ss.AndroidDriver(None)
    with pytest.raises(RuntimeError, match="not a debug build"):
        driver.reset_app()
    assert shell == []


def test_android_map_waits_for_its_tiles(monkeypatch) -> None:
    from meshtastic_mcp.emulator import avd

    calls: list[str] = []
    monkeypatch.setattr(avd, "first_emulator_serial", lambda: "emulator-5554")
    monkeypatch.setattr(avd, "resolve_package", lambda serial=None: "com.geeksville.mesh.debug")
    monkeypatch.setattr(avd, "deeplink", lambda path, **k: calls.append(f"open {path}"))
    monkeypatch.setattr(avd, "clear_logcat", lambda **k: calls.append("clear"))

    def poll(token, **k):
        calls.append(f"poll {k['tags']} {token}")
        return None

    monkeypatch.setattr(avd, "poll_logcat", poll)
    driver = ss.AndroidDriver(None)
    driver.open("nodes")
    assert driver.wait_ready() is True
    driver.open("map")
    assert driver.wait_ready() is False
    assert calls == ["open nodes", "clear", "open map", "poll ['MapDrawn'] tiles drawn"]
