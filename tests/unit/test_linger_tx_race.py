# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The port stays open after a write so queued RF and flash writes finish.

When a serial connection closes, DTR reset kills queued RF transmissions, and firmware queues
broadcasts behind a ~4 s channel-politeness delay. A text send holds the port for its linger;
a channel import closes with a nonzero linger; `connect()` clamps the linger it is given.
"""

from __future__ import annotations

import contextlib
import sys
import types

from meshtastic_mcp import connection as conn
from meshtastic_mcp.backends import legacy, python_send


class _FakeNode:
    def __init__(self):
        self.owner_set = False

    def setOwner(self, long_name, short_name):
        self.owner_set = True

    def _sendAdmin(self, msg):
        pass


class _FakeIface:
    def __init__(self):
        self.localNode = _FakeNode()
        self.close_called = False

    def close(self):
        self.close_called = True


def _send_text_holds(monkeypatch, **kwargs) -> list[float]:
    slept: list[float] = []

    class Iface:
        class myInfo:
            my_node_num = 1

        def sendData(self, *args, **kw):
            return types.SimpleNamespace(id=42)

    @contextlib.contextmanager
    def fake_connect(port=None, **kw):
        yield Iface()

    monkeypatch.setattr(conn, "connect", fake_connect)
    monkeypatch.setattr(python_send.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(python_send.time, "sleep", slept.append)
    legacy.send_text(text="test", port="/dev/ttyUSB0", **kwargs)
    return slept


def test_send_text_holds_the_port_for_tx_linger_s(monkeypatch):
    assert _send_text_holds(monkeypatch, tx_linger_s=7.5) == [7.5]


def test_send_text_defaults_to_8s_linger(monkeypatch):
    assert _send_text_holds(monkeypatch) == [8.0]


def test_set_channel_url_uses_linger(monkeypatch):
    """A channel import closes the port with a nonzero linger for the flash write."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})

        class FakeNode:
            channels = []

            def setURL(self, url, addOnly=False):
                pass

        class FakeIface:
            localNode = FakeNode()

        yield FakeIface()

    monkeypatch.setattr(conn, "connect", fake_connect)
    legacy.set_channel_url(url="https://meshtastic.org/e/#CgMSAQE")
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] > 0


def _drive_connect_linger(monkeypatch, requested_linger: float) -> list[float]:
    """Run the real `connect()` with a mocked serial stack, returning the
    actual durations passed to `time.sleep` (i.e. the effective linger)."""
    slept: list[float] = []
    monkeypatch.setattr(conn.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(conn, "resolve_port", lambda port: "/dev/ttyFAKE")
    monkeypatch.setattr(conn.registry, "active_session_for_port", lambda port: None)

    import threading

    monkeypatch.setattr(conn.registry, "port_lock", lambda port: threading.Lock())

    # `connect()` imports SerialInterface from meshtastic.serial_interface at call
    # time; inject a fake module so no real hardware is touched.
    fake_mod = types.ModuleType("meshtastic.serial_interface")
    fake_mod.SerialInterface = lambda **kwargs: _FakeIface()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic.serial_interface", fake_mod)

    with conn.connect(port="/dev/ttyFAKE", linger_s=requested_linger):
        pass
    return slept


def test_connect_clamps_excessive_linger(monkeypatch):
    """A caller-supplied linger above the cap is clamped so it can't pin the
    exclusive port lock indefinitely (CodeRabbit #18, Critical)."""
    slept = _drive_connect_linger(monkeypatch, 10_000.0)
    assert slept == [conn._MAX_LINGER_S], (
        f"expected linger clamped to {conn._MAX_LINGER_S}, slept {slept}"
    )


def test_connect_passes_normal_linger_through(monkeypatch):
    """A reasonable linger under the cap is used verbatim."""
    slept = _drive_connect_linger(monkeypatch, 8.0)
    assert slept == [8.0], f"expected linger 8.0 used as-is, slept {slept}"


def test_connect_negative_linger_never_sleeps(monkeypatch):
    """A negative linger is floored to 0 — no sleep at all."""
    slept = _drive_connect_linger(monkeypatch, -5.0)
    assert slept == [], f"expected no sleep for negative linger, slept {slept}"
