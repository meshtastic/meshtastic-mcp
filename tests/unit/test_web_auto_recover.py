# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the control-path auto-recover pre-flight (app._ensure_openable).

Pins the two gates that matter: it only escalates to ``port_recovery`` when even
a NON-exclusive open fails (so a port merely held by our own monitor is left
alone), and it never power-cycles while a test run owns the bus.
"""

from __future__ import annotations

import asyncio

import pytest

from meshtastic_mcp import port_recovery

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web import app
from meshtastic_mcp.web.services import test_runner


def _run(monkeypatch, *, openable, running=False):
    calls: list = []
    monkeypatch.setattr(port_recovery, "port_openable", lambda port, exclusive=True: openable)

    def _ensure(port, *, allow_power_cycle=True, **kw):
        calls.append({"port": port, "allow_power_cycle": allow_power_cycle})
        return port

    monkeypatch.setattr(port_recovery, "ensure_port_free", _ensure)
    test_runner._state["running"] = running
    try:
        asyncio.run(app._ensure_openable("/dev/cu.x"))
    finally:
        test_runner._state["running"] = False
    return calls


def test_skips_recovery_when_port_opens(monkeypatch):
    assert _run(monkeypatch, openable=(True, None)) == []


def test_recovers_when_nonexclusive_open_fails(monkeypatch):
    calls = _run(monkeypatch, openable=(False, OSError(22, "einval")))
    assert calls == [{"port": "/dev/cu.x", "allow_power_cycle": True}]


def test_no_power_cycle_during_a_run(monkeypatch):
    calls = _run(monkeypatch, openable=(False, OSError(22, "einval")), running=True)
    assert calls == [{"port": "/dev/cu.x", "allow_power_cycle": False}]


def test_recovery_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(
        port_recovery, "port_openable", lambda port, exclusive=True: (False, OSError(22))
    )

    def _boom(port, **kw):
        raise port_recovery.PortRecoveryError("nope")

    monkeypatch.setattr(port_recovery, "ensure_port_free", _boom)
    # Must not raise.
    asyncio.run(app._ensure_openable("/dev/cu.x"))
