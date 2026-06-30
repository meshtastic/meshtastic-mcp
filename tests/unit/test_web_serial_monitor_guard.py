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
