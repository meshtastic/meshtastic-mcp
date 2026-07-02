"""registry.clear_port_lock recovers a leaked per-port lock.

When a connect() is abandoned mid-flight (its thread stuck in meshtastic's
unbounded TX-queue drain), the per-port lock never gets released and blocks every
later in-process connect() on that port. clear_port_lock drops the entry so the
next port_lock() mints a fresh, unheld one.
"""

from __future__ import annotations

from meshtastic_mcp import registry


def test_clear_port_lock_recovers_a_leaked_lock():
    port = "/dev/cu.faketest-leak"
    leaked = registry.port_lock(port)
    assert leaked.acquire(blocking=False)  # a "stuck thread" now holds it
    try:
        # Same lock is handed out + still held → a new connect would fail.
        assert registry.port_lock(port) is leaked
        assert not registry.port_lock(port).acquire(blocking=False)

        # Clear it → next caller gets a fresh, acquirable lock.
        registry.clear_port_lock(port)
        fresh = registry.port_lock(port)
        assert fresh is not leaked
        assert fresh.acquire(blocking=False)
        fresh.release()
    finally:
        leaked.release()  # we own this one (acquired on this thread) — clean up


def test_clear_port_lock_is_a_noop_for_unknown_port():
    registry.clear_port_lock("/dev/cu.never-seen")  # must not raise
