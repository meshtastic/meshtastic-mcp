"""Per-device port arbitration (PortLocks.guard).

Verifies the guard's three contract points with a fake serial monitor and
``asyncio.run``: same-serial access is mutually exclusive, the device's monitor
is suspended for the body and resumed even on error, and different serials run
concurrently (the lock is per-device, not global).
"""

from __future__ import annotations

import asyncio

import pytest

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
