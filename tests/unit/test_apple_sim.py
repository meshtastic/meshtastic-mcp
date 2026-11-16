# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Portable unit tests for the Apple-simulator app-plane wrapper (no Xcode/simulator needed)."""

from __future__ import annotations

import json
import subprocess

from meshtastic_mcp.emulator import apple_sim


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_tcp_dut_address_uses_localhost() -> None:
    # Unlike Android's 10.0.2.2, the iOS Simulator shares the host network stack.
    assert apple_sim.tcp_dut_address(4403) == "127.0.0.1:4403"
    assert apple_sim.SIM_HOST_ALIAS == "127.0.0.1"


def test_list_simulators_flattens_runtimes(monkeypatch) -> None:
    payload = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                {"udid": "AAA", "name": "iPhone 17 Pro", "state": "Booted"},
                {"udid": "BBB", "name": "iPhone 17e", "state": "Shutdown"},
            ]
        }
    }
    monkeypatch.setattr(apple_sim, "simctl", lambda *a, **k: _cp(json.dumps(payload)))
    sims = apple_sim.list_simulators()
    assert {s["udid"] for s in sims} == {"AAA", "BBB"}
    assert all("runtime" in s for s in sims)


def test_booted_udid(monkeypatch) -> None:
    payload = {"devices": {"rt": [{"udid": "AAA", "name": "iPhone", "state": "Booted"}]}}
    monkeypatch.setattr(apple_sim, "simctl", lambda *a, **k: _cp(json.dumps(payload)))
    assert apple_sim.booted_udid() == "AAA"


def test_ui_dump_parses_array_and_ndjson(monkeypatch) -> None:
    monkeypatch.setattr(apple_sim, "idb", lambda *a, **k: _cp('[{"AXLabel": "Nodes 2/2"}]'))
    assert apple_sim.ui_dump()[0]["AXLabel"] == "Nodes 2/2"
    monkeypatch.setattr(apple_sim, "idb", lambda *a, **k: _cp('{"AXLabel": "a"}\n{"AXValue": "b"}'))
    assert len(apple_sim.ui_dump()) == 2


def test_find_text_across_accessibility_fields(monkeypatch) -> None:
    monkeypatch.setattr(apple_sim, "idb", lambda *a, **k: _cp('[{"AXValue": "E2E-123"}]'))
    assert apple_sim.find_text("E2E-123") is True
    assert apple_sim.find_text("nope") is False


def _record_simctl(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _cp("")

    monkeypatch.setattr(apple_sim, "simctl", fake)
    return calls


def test_set_appearance_validates_and_targets_device(monkeypatch) -> None:
    calls = _record_simctl(monkeypatch)
    apple_sim.set_appearance("dark", udid="AAA")
    assert calls == [("ui", "AAA", "appearance", "dark")]
    try:
        apple_sim.set_appearance("sepia")
    except apple_sim.AppleSimError:
        pass
    else:
        raise AssertionError("an unknown appearance must raise")
    assert len(calls) == 1


def test_clean_status_bar_pins_time(monkeypatch) -> None:
    calls = _record_simctl(monkeypatch)
    apple_sim.clean_status_bar()
    args = calls[0]
    assert args[:3] == ("status_bar", "booted", "override")
    assert args[args.index("--time") + 1] == "9:41"


def test_connect_link_points_at_host_node() -> None:
    assert (
        apple_sim.connect_link(4403)
        == "meshtastic://meshtastic/connections?address=t127.0.0.1:4403"
    )


def test_open_url_uses_simctl_openurl(monkeypatch) -> None:
    calls = _record_simctl(monkeypatch)
    apple_sim.open_url("meshtastic://meshtastic/messages", udid="AAA")
    assert calls == [("openurl", "AAA", "meshtastic://meshtastic/messages")]


def test_ensure_booted_without_idb_skips_companion(monkeypatch) -> None:
    payload = {"devices": {"rt": [{"udid": "BBB", "name": "iPhone 17", "state": "Shutdown"}]}}
    monkeypatch.setattr(apple_sim, "simctl", lambda *a, **k: _cp(json.dumps(payload)))
    monkeypatch.setattr(apple_sim, "_run", lambda *a, **k: _cp(""))
    monkeypatch.setattr(apple_sim, "_xcrun_bin", lambda: "xcrun")
    monkeypatch.setattr(apple_sim, "has_idb", lambda: False)

    def no_companion(udid: str, **kwargs: object) -> int:
        raise AssertionError("idb_companion must not start without idb")

    monkeypatch.setattr(apple_sim, "start_companion", no_companion)
    assert apple_sim.ensure_booted("iPhone") == "BBB"


def test_ensure_companion_reuses_a_working_one(monkeypatch) -> None:
    # A companion another process started already answers UI calls; a second one would exit.
    monkeypatch.setattr(apple_sim, "has_idb", lambda: True)
    monkeypatch.setattr(apple_sim.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(apple_sim, "idb", lambda *a, **k: _cp("[]"))

    def no_companion(udid: str, **kwargs: object) -> int:
        raise AssertionError("must reuse the companion that already answers")

    monkeypatch.setattr(apple_sim, "start_companion", no_companion)
    apple_sim._ensure_companion("AAA")


def test_ensure_companion_starts_one_when_ui_calls_fail(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(apple_sim, "has_idb", lambda: True)
    monkeypatch.setattr(apple_sim.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(apple_sim, "idb", lambda *a, **k: _cp("", returncode=1))
    monkeypatch.setattr(apple_sim, "start_companion", lambda udid, **k: started.append(udid) or 1)
    apple_sim._ensure_companion("AAA")
    assert started == ["AAA"]
