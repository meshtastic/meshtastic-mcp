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
from typing import Protocol

# How long guard() waits for an abandoned (wedged) reader thread to die after
# suspend, before refusing the port. Module constant so tests can shrink it.
WEDGE_WAIT_S = 8.0
_WEDGE_POLL_S = 0.25
# Upper bound on wait_clear(): how long a claimant waits for an in-flight guard
# to release the port before treating the device as unavailable. Generous enough
# to cover a normal port operation (enrichment/keep-alive, a few seconds), short
# enough that one stuck guard can't stall a fleet-wide claim sweep.
WAIT_CLEAR_S = 30.0


class PortWedgedError(RuntimeError):
    """The device's port is still held by an abandoned serial-reader thread
    (a close timed out mid-kernel-read — see SerialMonitor.is_wedged). Opening
    a second reader now would interleave two consumers on one tty and corrupt
    the protobuf stream, so the caller must fail cleanly instead; a
    power-cycle/unwedge (device re-enumeration) frees the port."""


class PortClaimedError(RuntimeError):
    """The device is claimed for an extended window by a long-running owner
    (e.g. the nightly soak's persistent API observer). Fail fast — matching
    the port-arbitration philosophy — instead of queueing behind a claim
    that can last hours."""


class PortClaimLookup(Protocol):
    """The slice of PortLocks a claim-aware consumer needs: 'who holds this
    device, if anyone'. Consumers depend on this instead of the whole class so
    the contract is type-checked (and test doubles stay trivial)."""

    def claimed_by(self, serial: str) -> str | None: ...


class PortLocks:
    def __init__(self, serialmon=None) -> None:
        self.serialmon = serialmon
        self._locks: dict[str, asyncio.Lock] = {}
        self._claims: dict[str, str] = {}  # serial -> owner label

    def claim(self, serial: str, owner: str) -> None:
        """Reserve a device for a long-lived owner. guard() refuses the serial
        until release_claim(). Raises PortClaimedError if already claimed by a
        different owner. Claiming does NOT free the port — the claimant must
        suspend the serial monitor itself before opening anything."""
        current = self._claims.get(serial)
        if current is not None and current != owner:
            raise PortClaimedError(f"{serial}: already claimed by {current}")
        self._claims[serial] = owner

    def release_claim(self, serial: str, owner: str) -> None:
        """Release a claim. Only the claiming owner may release; a mismatched
        release is ignored (the claim stays) so a stale caller can't strip an
        active owner's reservation."""
        if self._claims.get(serial) == owner:
            self._claims.pop(serial, None)

    def claimed_by(self, serial: str) -> str | None:
        return self._claims.get(serial)

    async def wait_clear(self, serial: str, timeout_s: float = WAIT_CLEAR_S) -> bool:
        """Wait (bounded) for any in-flight guard() on this serial to finish.
        Returns True when the port is clear, False on timeout.

        claim() stops NEW guards immediately, but a guard already inside its
        body (e.g. enrichment mid-device_info) still holds the port; a claimant
        that opens without waiting loses the race with a busy error. Taking and
        releasing the per-serial lock is exactly 'the in-flight guard is done'.

        Bounded on purpose: a guard body can be arbitrarily long (a flash, a
        recovery reflash) or leaked outright, and blocking here would stall the
        caller — the soak claims every board before opening any observer, so one
        stuck guard would hold up the whole fleet's startup. Contention resolves
        to a retryable 'unavailable', matching the fail-fast port philosophy."""
        try:
            async with asyncio.timeout(timeout_s):
                async with self._lock(serial):
                    return True
        except TimeoutError:
            return False

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
        owner = self._claims.get(serial)
        if owner is not None:
            raise PortClaimedError(f"{serial}: device is held by {owner} — retry after it ends")
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
