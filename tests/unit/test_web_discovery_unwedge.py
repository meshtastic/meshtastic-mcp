# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Discovery's self-heal: power-cycle a genuinely wedged device — and ONLY then.

The dangerous part of background auto-recovery is firing it when it shouldn't
(during a run, on a healthy-but-busy device, or on a port merely held by our own
monitor). These tests pin the gates: only an EINVAL (wedged) port, never during a
test run, hub-slot required, cooldown-limited.
"""

from __future__ import annotations

import asyncio

import pytest

from meshtastic_mcp import port_recovery

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services import discovery as disc
from meshtastic_mcp.web.services import power, test_runner


class _Hub:
    async def publish(self, *a, **k):
        pass


def _disc():
    return disc.DeviceDiscovery(db=object(), hub=_Hub(), serialmon=None)


_ROW = {
    "serial_number": "S1",
    "hub_location": "20-3",
    "hub_port": 5,
    "current_port": "/dev/cu.x",
    "hw_model": "HELTEC_V3",
}


def _run(monkeypatch, d, *, openable, running=False, auto=True, row=_ROW):
    cycled: list = []
    monkeypatch.setattr(power, "power_slot", lambda loc, p, a: cycled.append((loc, p, a)))
    monkeypatch.setattr(port_recovery, "port_openable", lambda port, exclusive=True: openable)
    test_runner._state["running"] = running
    d.auto_unwedge = auto
    try:
        asyncio.run(d._maybe_unwedge("S1", row))
    finally:
        test_runner._state["running"] = False
    return cycled


def test_fires_on_einval_wedge(monkeypatch):
    cycled = _run(monkeypatch, _disc(), openable=(False, OSError(22, "einval")))
    assert cycled == [("20-3", 5, "cycle")]


def test_skips_held_port_eagain(monkeypatch):
    # Held (errno 35) — could be our own monitor; never power-cycle.
    assert _run(monkeypatch, _disc(), openable=(False, OSError(35, "busy"))) == []


def test_skips_openable_device(monkeypatch):
    assert _run(monkeypatch, _disc(), openable=(True, None)) == []


def test_never_fires_during_a_run(monkeypatch):
    assert _run(monkeypatch, _disc(), openable=(False, OSError(22, "x")), running=True) == []


def test_disabled_by_flag(monkeypatch):
    assert _run(monkeypatch, _disc(), openable=(False, OSError(22, "x")), auto=False) == []


def test_skips_without_hub_slot(monkeypatch):
    row = {**_ROW, "hub_location": None, "hub_port": None}
    assert _run(monkeypatch, _disc(), openable=(False, OSError(22, "x")), row=row) == []


def test_cooldown_prevents_refire(monkeypatch):
    d = _disc()
    first = _run(monkeypatch, d, openable=(False, OSError(22, "x")))
    assert first == [("20-3", 5, "cycle")]
    # Immediately again — cooldown should suppress it.
    second = _run(monkeypatch, d, openable=(False, OSError(22, "x")))
    assert second == []
