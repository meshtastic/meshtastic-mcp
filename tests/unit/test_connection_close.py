# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The bounded close — a graceful `iface.close()` must never hang the caller.

After a factory_reset/reboot the meshtastic library's close() loops forever
waiting for TX-queue space that never frees (a real 600s test hang). `connect`
now closes on a daemon thread and abandons it after a short timeout.
"""

from __future__ import annotations

import threading
import time

from meshtastic_mcp import connection as conn


def test_close_bounded_returns_despite_hanging_close(monkeypatch):
    monkeypatch.setattr(conn, "_CLOSE_TIMEOUT_S", 0.5)
    started = threading.Event()

    class HangingIface:
        def close(self):
            started.set()
            time.sleep(30)  # the pathological reboot-mid-close hang

    t0 = time.monotonic()
    conn._close_bounded(HangingIface())
    elapsed = time.monotonic() - t0
    assert started.wait(1), "close() was never invoked"
    assert elapsed < 3, f"_close_bounded blocked for {elapsed}s — not bounded"


def test_close_bounded_completes_fast_close(monkeypatch):
    monkeypatch.setattr(conn, "_CLOSE_TIMEOUT_S", 5.0)
    closed = threading.Event()

    class FastIface:
        def close(self):
            closed.set()

    conn._close_bounded(FastIface())
    assert closed.wait(1)


def test_close_bounded_swallows_close_errors():
    class BoomIface:
        def close(self):
            raise RuntimeError("boom")

    # Must not raise — a close error is best-effort cleanup.
    conn._close_bounded(BoomIface())
