# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The unwedging primitive — probe → diagnose → power-cycle → re-resolve.

All hardware (pyserial open, uhubctl, lsof) is mocked, so this runs in the unit
tier. Covers the escalation logic that decides whether to power-cycle and how it
re-finds a re-enumerated device by hub slot.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp import port_recovery as pr


class _FakePort:
    def __init__(self, device: str, location: str | None) -> None:
        self.device = device
        self.location = location


def _mock_comports(monkeypatch, ports):
    import serial.tools.list_ports as lp

    monkeypatch.setattr(lp, "comports", lambda: ports)


def test_hub_slot_parsing(monkeypatch):
    _mock_comports(
        monkeypatch,
        [_FakePort("/dev/cu.usbserial-0001", "20-3.5"), _FakePort("/dev/cu.x", None)],
    )
    assert pr.hub_slot_for_port("/dev/cu.usbserial-0001") == ("20-3", 5)
    assert pr.hub_slot_for_port("/dev/cu.x") == (None, None)  # no location
    assert pr.hub_slot_for_port("/dev/cu.missing") == (None, None)


def test_port_on_slot_refinds_by_location(monkeypatch):
    _mock_comports(monkeypatch, [_FakePort("/dev/cu.usbmodemNEW", "20-3.1")])
    assert pr.port_on_slot("20-3", 1) == "/dev/cu.usbmodemNEW"
    assert pr.port_on_slot("20-3", 9) is None


def test_who_holds_port_parses_lsof(monkeypatch):
    class _Res:
        stdout = "COMMAND PID USER FD\nPython 123 me 7u\nesptool 456 me 3u\n"

    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Res())
    assert pr.who_holds_port("/dev/x") == [("Python", "123"), ("esptool", "456")]


def test_ensure_returns_immediately_when_openable(monkeypatch):
    monkeypatch.setattr(pr, "port_openable", lambda *a, **k: (True, None))
    assert pr.ensure_port_free("/dev/cu.x", role="nrf52") == "/dev/cu.x"


def test_ensure_raises_when_no_hub_slot(monkeypatch):
    monkeypatch.setattr(pr, "port_openable", lambda *a, **k: (False, OSError(35, "busy")))
    monkeypatch.setattr(pr, "who_holds_port", lambda p: [("Python", "1")])
    monkeypatch.setattr(pr, "hub_slot_for_port", lambda p: (None, None))
    with pytest.raises(pr.PortRecoveryError, match="hub slot can't be resolved"):
        pr.ensure_port_free("/dev/cu.x", role="nrf52", wait_s=0.05, poll=0.01)


def test_ensure_power_cycles_and_recovers(monkeypatch):
    """Wedged port → power-cycle its slot → device re-enumerates on a NEW path →
    that path opens → return it."""
    calls = {"cycle": 0}
    state = {"open_after_cycle": False}

    def fake_openable(port, **k):
        if port == "/dev/cu.OLD":
            return (False, OSError(22, "Invalid argument"))  # wedged
        if port == "/dev/cu.NEW":
            return (
                state["open_after_cycle"],
                None if state["open_after_cycle"] else OSError(35, "busy"),
            )
        return (False, OSError(2, "nope"))

    def fake_cycle(hub, hub_port, **k):
        calls["cycle"] += 1
        calls["slot"] = (hub, hub_port)
        state["open_after_cycle"] = True  # device comes back healthy

    monkeypatch.setattr(pr, "port_openable", fake_openable)
    monkeypatch.setattr(pr, "who_holds_port", lambda p: [])
    monkeypatch.setattr(pr, "hub_slot_for_port", lambda p: ("20-3", 5))
    monkeypatch.setattr(pr, "port_on_slot", lambda hub, hp: "/dev/cu.NEW")
    monkeypatch.setattr(pr, "_uhubctl_available", lambda: True)
    monkeypatch.setattr(pr.uhubctl, "cycle", fake_cycle)

    out = pr.ensure_port_free(
        "/dev/cu.OLD", role="esp32s3", wait_s=0.05, poll=0.01, reenum_timeout_s=2.0
    )
    assert out == "/dev/cu.NEW"
    assert calls["cycle"] == 1 and calls["slot"] == ("20-3", 5)


def test_ensure_no_power_cycle_when_disabled(monkeypatch):
    monkeypatch.setattr(pr, "port_openable", lambda *a, **k: (False, OSError(35, "busy")))
    monkeypatch.setattr(pr, "who_holds_port", lambda p: [])
    with pytest.raises(pr.PortRecoveryError, match="power-cycle disabled"):
        pr.ensure_port_free(
            "/dev/cu.x", role="nrf52", wait_s=0.05, poll=0.01, allow_power_cycle=False
        )
