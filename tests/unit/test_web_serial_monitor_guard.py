# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The server must vacate the serial bus during a test run.

A test run's pytest subprocess opens the device ports exclusively (flash,
transmit-history reset, …). If the server re-opens a port mid-run — e.g. the
Datadog fleet-log capture re-acquiring a monitor on the next discovery scan —
pytest fails with "Could not exclusively lock port". serial_monitor._open
refuses to open any port while a run is active; resume_all re-establishes them
after.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services import serial_monitor as sm
from meshtastic_mcp.web.services import test_runner as tr


def test_monitor_refuses_to_open_a_port_during_a_run(monkeypatch):
    # Don't touch a real serial device — just confirm a reader thread is/ isn't
    # spawned.
    monkeypatch.setattr(sm.SerialMonitor, "_read_loop", lambda *a, **k: None)

    async def fake_get(_db, serial):
        return {"serial_number": serial, "kind": "usb", "current_port": "/dev/fake0"}

    monkeypatch.setattr(sm.rd, "get", fake_get)

    class _Hub:
        def publish_threadsafe(self, *a, **k):
            pass

        async def publish(self, *a, **k):
            pass

    mon = sm.SerialMonitor(db=object(), hub=_Hub())

    async def go():
        tr._state["running"] = True
        # A capture/UI acquire during the run must NOT open the port.
        await mon.acquire("S1")
        assert mon._mons["S1"].thread is None, "opened a serial port mid-run!"

        # When the run ends, resume_all re-establishes the monitor.
        tr._state["running"] = False
        await mon.resume_all()
        assert mon._mons["S1"].thread is not None, "did not re-open after the run"
        await mon.shutdown()

    try:
        asyncio.run(go())
    finally:
        tr._state["running"] = False


class _FakeThread:
    """Stands in for a reader thread; `alive` is flipped by the test to
    simulate the wedged reader finally dying (e.g. after a power-cycle
    re-enumerates the device and its blocked ioctl errors out)."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive

    def join(self, _timeout: float | None = None) -> None:
        pass


def test_abandoned_close_self_heals_once_the_reader_dies(monkeypatch):
    """Regression: a timed-out monitor close abandoned the reader thread and
    left mon.thread set forever — resume() could never reopen the monitor and
    the process's own dead fd kept the port EIO-wedged with no recovery short
    of a restart (found live: /dev/ttyACM2 on a 4-board bench). A dead
    abandoned thread must now count as closed."""
    monkeypatch.setattr(sm.SerialMonitor, "_read_loop", lambda *a, **k: None)

    async def fake_get(_db, serial):
        return {"serial_number": serial, "kind": "usb", "current_port": "/dev/fake0"}

    monkeypatch.setattr(sm.rd, "get", fake_get)

    class _Hub:
        def publish_threadsafe(self, *a, **k):
            pass

        async def publish(self, *a, **k):
            pass

    mon = sm.SerialMonitor(db=object(), hub=_Hub())

    async def go():
        await mon.acquire("S1")
        state = mon._mons["S1"]
        assert state.thread is not None

        # Wedge it: the reader refuses to die within _close's join timeout.
        wedged = _FakeThread(alive=True)
        state.thread = wedged  # type: ignore[assignment]
        await mon.suspend("S1")
        assert state.thread is wedged, "guard must keep the abandoned handle"
        assert mon.is_wedged("S1"), "a live abandoned reader is a self-wedge"

        # While the reader is genuinely alive, resume must NOT double-open.
        await mon.resume("S1")
        assert state.thread is wedged, "spawned a second reader on a held port!"

        # The reader dies (power-cycle → re-enumeration → read errors out).
        await mon.suspend("S1")
        wedged.alive = False
        assert not mon.is_wedged("S1")

        # resume() now self-heals: stale handle cleared, fresh reader spawned.
        await mon.resume("S1")
        assert state.thread is not None and state.thread is not wedged, (
            "monitor did not reopen after the abandoned reader died"
        )
        await mon.shutdown()

    asyncio.run(go())


def test_monitor_refuses_to_open_a_claimed_port(monkeypatch):
    """While the nightly soak's API observer claims a device, the monitor must
    not open a raw reader against it (POSIX ttys allow double-opens; a second
    reader would steal bytes from the held protobuf stream). After the claim is
    released, resume re-establishes the monitor."""
    monkeypatch.setattr(sm.SerialMonitor, "_read_loop", lambda *a, **k: None)

    async def fake_get(_db, serial):
        return {"serial_number": serial, "kind": "usb", "current_port": "/dev/fake0"}

    monkeypatch.setattr(sm.rd, "get", fake_get)

    published: list[tuple[str, dict]] = []

    class _Hub:
        def publish_threadsafe(self, topic, data):
            published.append((topic, data))

        async def publish(self, *a, **k):
            pass

    from meshtastic_mcp.web.services.portlock import PortLocks

    mon = sm.SerialMonitor(db=object(), hub=_Hub())
    locks = PortLocks(serialmon=mon)
    mon.portlocks = locks

    async def go():
        locks.claim("S1", "nightly soak")
        await mon.acquire("S1")
        assert mon._mons["S1"].thread is None, "opened a raw reader on a claimed port!"
        # The UI tab is told why its monitor shows no reader of its own.
        assert any("nightly soak" in d.get("line", "") for _t, d in published)

        locks.release_claim("S1", "nightly soak")
        await mon.resume("S1")
        assert mon._mons["S1"].thread is not None, "did not re-open after claim release"
        await mon.shutdown()

    asyncio.run(go())
