# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Per-device port arbitration.

Every port-bound operation — auto-enrichment, screen keep-alive, and the control
actions (flash/reboot/config/send-text/…) — opens a connection to the device.
Without coordination two of them can try to open the same serial port at once
(the OS allows only one), so one fails and retries. ``PortLocks.guard(serial)``
gives each device a single async lock AND frees its live serial monitor for the
duration, so callers just do::

    async with portlocks.guard(serial):
        await asyncio.to_thread(admin.do_something, port)

Different devices still run concurrently — the lock is per serial, not global.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class PortLocks:
    def __init__(self, serialmon=None) -> None:
        self.serialmon = serialmon
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, serial: str) -> asyncio.Lock:
        lk = self._locks.get(serial)
        if lk is None:
            lk = self._locks[serial] = asyncio.Lock()
        return lk

    @asynccontextmanager
    async def guard(self, serial: str) -> AsyncIterator[None]:
        """Hold exclusive access to a device's port: serialise against other
        port operations on the same serial and suspend its serial monitor for
        the duration (resumed on exit, even on error)."""
        async with self._lock(serial):
            if self.serialmon is not None:
                await self.serialmon.suspend(serial)
            try:
                yield
            finally:
                if self.serialmon is not None:
                    await self.serialmon.resume(serial)
