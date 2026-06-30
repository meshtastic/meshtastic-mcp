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
