# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Per-device port arbitration (PortLocks.guard).

Verifies the guard's three contract points with a fake serial monitor and
``asyncio.run``: same-serial access is mutually exclusive, the device's monitor
is suspended for the body and resumed even on error, and different serials run
concurrently (the lock is per-device, not global).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services.portlock import PortLocks


class _FakeMonitor:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def suspend(self, serial: str) -> None:
        self.events.append(("suspend", serial))

    async def resume(self, serial: str) -> None:
        self.events.append(("resume", serial))


def test_same_serial_is_mutually_exclusive():
    async def go():
        pl = PortLocks(serialmon=_FakeMonitor())
        order: list[str] = []

        async def worker(tag: str):
            async with pl.guard("DEV"):
                order.append(f"{tag}-in")
                await asyncio.sleep(0.02)  # hold the port
                order.append(f"{tag}-out")

        await asyncio.gather(worker("a"), worker("b"))
        # Whichever ran first fully entered+exited before the other entered —
        # no interleaving of the critical sections.
        assert order in (
            ["a-in", "a-out", "b-in", "b-out"],
            ["b-in", "b-out", "a-in", "a-out"],
        ), order

    asyncio.run(go())


def test_suspends_and_resumes_monitor_even_on_error():
    async def go():
        mon = _FakeMonitor()
        pl = PortLocks(serialmon=mon)

        with pytest.raises(RuntimeError):
            async with pl.guard("DEV"):
                raise RuntimeError("boom")

        assert mon.events == [("suspend", "DEV"), ("resume", "DEV")]
        # Lock is released after an error — the next guard still works.
        async with pl.guard("DEV"):
            pass
        assert mon.events[-2:] == [("suspend", "DEV"), ("resume", "DEV")]

    asyncio.run(go())


def test_different_serials_run_concurrently():
    async def go():
        pl = PortLocks(serialmon=_FakeMonitor())
        both_in = asyncio.Event()
        count = 0

        async def worker(serial: str):
            nonlocal count
            async with pl.guard(serial):
                count += 1
                if count == 2:
                    both_in.set()
                # If the lock were global, the second worker could never enter
                # while the first holds it — this would deadlock the wait.
                await asyncio.wait_for(both_in.wait(), timeout=1.0)

        await asyncio.gather(worker("A"), worker("B"))
        assert count == 2

    asyncio.run(go())


def test_no_monitor_is_a_noop_guard():
    async def go():
        pl = PortLocks(serialmon=None)
        async with pl.guard("DEV"):
            pass  # must not raise when there's no monitor to suspend

    asyncio.run(go())


class _WedgedMonitor(_FakeMonitor):
    """Reports a wedged (abandoned-alive) reader for the first N polls."""

    def __init__(self, wedged_polls: int) -> None:
        super().__init__()
        self._left = wedged_polls

    def is_wedged(self, serial: str) -> bool:
        if self._left > 0:
            self._left -= 1
            return True
        return False


def test_guard_refuses_a_wedged_port(monkeypatch):
    """A reader that survives suspend means the port is NOT free — guard must
    fail fast with PortWedgedError instead of letting the caller open a second
    reader and corrupt the stream. The monitor is still resumed."""
    from meshtastic_mcp.web.services import portlock as pl_mod
    from meshtastic_mcp.web.services.portlock import PortWedgedError

    monkeypatch.setattr(pl_mod, "WEDGE_WAIT_S", 0.05)
    monkeypatch.setattr(pl_mod, "_WEDGE_POLL_S", 0.01)

    async def go():
        mon = _WedgedMonitor(wedged_polls=10_000)  # never clears
        pl = PortLocks(serialmon=mon)
        entered = False
        with pytest.raises(PortWedgedError):
            async with pl.guard("DEV"):
                entered = True
        assert not entered  # the body must never run against a held port
        assert mon.events == [("suspend", "DEV"), ("resume", "DEV")]

    asyncio.run(go())


def test_guard_proceeds_once_the_wedged_reader_dies(monkeypatch):
    """A transiently-wedged reader (dies during the wait window) must not fail
    the guard — it proceeds as soon as the port is genuinely free."""
    from meshtastic_mcp.web.services import portlock as pl_mod

    monkeypatch.setattr(pl_mod, "WEDGE_WAIT_S", 1.0)
    monkeypatch.setattr(pl_mod, "_WEDGE_POLL_S", 0.01)

    async def go():
        mon = _WedgedMonitor(wedged_polls=3)  # clears on the 4th poll
        pl = PortLocks(serialmon=mon)
        entered = False
        async with pl.guard("DEV"):
            entered = True
        assert entered
        assert mon.events == [("suspend", "DEV"), ("resume", "DEV")]

    asyncio.run(go())
