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

# How long guard() waits for an abandoned (wedged) reader thread to die after
# suspend, before refusing the port. Module constant so tests can shrink it.
WEDGE_WAIT_S = 8.0
_WEDGE_POLL_S = 0.25


class PortWedgedError(RuntimeError):
    """The device's port is still held by an abandoned serial-reader thread
    (a close timed out mid-kernel-read — see SerialMonitor.is_wedged). Opening
    a second reader now would interleave two consumers on one tty and corrupt
    the protobuf stream, so the caller must fail cleanly instead; a
    power-cycle/unwedge (device re-enumeration) frees the port."""


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
        the duration (resumed on exit, even on error).

        If the monitor's reader thread survived suspend (abandoned by a timed
        out close, still blocked in a kernel read), the port is NOT actually
        free: a second reader would steal bytes from the first and the
        meshtastic handshake fails with corrupt-protobuf / "multiple access on
        port" symptoms (observed as soak-preflight config-read timeouts). Wait
        briefly for the reader to die, then refuse with PortWedgedError rather
        than proceed and corrupt."""
        async with self._lock(serial):
            if self.serialmon is not None:
                await self.serialmon.suspend(serial)
            try:
                is_wedged = getattr(self.serialmon, "is_wedged", None)
                if is_wedged is not None and is_wedged(serial):
                    deadline = asyncio.get_running_loop().time() + WEDGE_WAIT_S
                    while is_wedged(serial):
                        if asyncio.get_running_loop().time() >= deadline:
                            raise PortWedgedError(
                                f"{serial}: port still held by a wedged serial reader "
                                "— refusing to open a second reader (it would corrupt "
                                "the stream); power-cycle/unwedge the device to free it"
                            )
                        await asyncio.sleep(_WEDGE_POLL_S)
                yield
            finally:
                if self.serialmon is not None:
                    await self.serialmon.resume(serial)
