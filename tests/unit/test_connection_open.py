# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Opening a serial interface is bounded: a wedged board fails the call instead of hanging it.

An nRF52 whose USB CDC has wedged can block `SerialInterface` construction past its own
`timeout` (the reply deadline only covers `waitForConfig`); a real `device_info` sat for 19
minutes. `connect` now gives up after the timeout plus a grace period.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from meshtastic_mcp import connection as conn

PORT = "/dev/ttyFAKE0"


@pytest.fixture
def fake_serial(monkeypatch):
    """Install a fake `meshtastic.serial_interface.SerialInterface` whose open can be held."""
    release = threading.Event()
    opened: list[object] = []
    closed: list[object] = []

    class FakeIface:
        def __init__(self, devPath, connectNow, noProto, timeout):
            if hold["on"]:
                release.wait(30)
            opened.append(self)

        def close(self):
            closed.append(self)

    hold = {"on": True}
    module = types.ModuleType("meshtastic.serial_interface")
    module.SerialInterface = FakeIface
    monkeypatch.setitem(sys.modules, "meshtastic.serial_interface", module)
    monkeypatch.setattr(conn, "resolve_port", lambda port: port)
    monkeypatch.setattr(conn, "_OPEN_GRACE_S", 0.3)
    monkeypatch.setattr(conn, "_CLOSE_TIMEOUT_S", 1.0)
    monkeypatch.setattr(conn, "_wedged_opens", {})
    yield types.SimpleNamespace(release=release, opened=opened, closed=closed, hold=hold)
    release.set()


def test_a_wedged_open_fails_the_call_within_the_deadline(fake_serial) -> None:
    t0 = time.monotonic()
    with pytest.raises(conn.ConnectionError, match=r"(?i)power-cycle"):
        with conn.connect(PORT, timeout_s=0.5):
            pass
    assert time.monotonic() - t0 < 3


def test_a_port_with_a_stuck_open_fails_fast_until_it_finishes(fake_serial) -> None:
    with pytest.raises(conn.ConnectionError):
        with conn.connect(PORT, timeout_s=0.5):
            pass
    t0 = time.monotonic()
    with pytest.raises(conn.ConnectionError, match="still stuck"):
        with conn.connect(PORT, timeout_s=0.5):
            pass
    assert time.monotonic() - t0 < 0.5


def test_a_late_open_is_closed_and_the_port_is_usable_again(fake_serial) -> None:
    with pytest.raises(conn.ConnectionError):
        with conn.connect(PORT, timeout_s=0.5):
            pass
    fake_serial.release.set()
    for _ in range(50):
        if fake_serial.closed:
            break
        time.sleep(0.05)
    assert fake_serial.closed == fake_serial.opened, "the abandoned interface was not closed"
    fake_serial.hold["on"] = False
    with conn.connect(PORT, timeout_s=0.5) as iface:
        assert iface is fake_serial.opened[-1]


def test_a_healthy_open_is_unchanged(fake_serial) -> None:
    fake_serial.hold["on"] = False
    with conn.connect(PORT, timeout_s=0.5) as iface:
        assert iface is fake_serial.opened[0]
    assert fake_serial.closed == [iface]


def test_an_open_that_raises_still_raises(fake_serial, monkeypatch) -> None:
    class Boom:
        def __init__(self, **_kw):
            raise RuntimeError("Timed out waiting for connection completion")

    sys.modules["meshtastic.serial_interface"].SerialInterface = Boom
    with pytest.raises(RuntimeError, match="Timed out waiting"):
        with conn.connect(PORT, timeout_s=0.5):
            pass
