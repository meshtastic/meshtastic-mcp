# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Test linger_s parameter to prevent serial TX race.

When a serial connection closes, DTR reset kills queued RF transmissions.
Meshtastic firmware queues broadcasts behind ~4s channel-politeness delay.
The linger_s parameter delays close until queued TX completes.

Tests verify:
  - send_text() passes tx_linger_s through to connect()
  - set_owner() uses a nonzero linger
  - set_config() uses a nonzero linger
  - set_channel_url() uses a nonzero linger
"""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from meshtastic_mcp import admin
from meshtastic_mcp import connection as conn


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


def test_send_text_passes_tx_linger_s(monkeypatch):
    """send_text() must pass tx_linger_s through to connect()."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})

        class FakeIface:
            def sendText(self, *args, **kwargs):
                class Packet:
                    id = 42

                return Packet()

        yield FakeIface()

    monkeypatch.setattr(admin, "connect", fake_connect)

    # Call send_text with a custom tx_linger_s
    admin.send_text(text="test", port="/dev/ttyUSB0", tx_linger_s=7.5)

    # Verify connect was called with the tx_linger_s value
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] == 7.5, f"Expected linger_s=7.5, got {connect_calls[0]}"


def test_send_text_defaults_to_8s_linger(monkeypatch):
    """send_text() must default to tx_linger_s=8.0."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})

        class FakeIface:
            def sendText(self, *args, **kwargs):
                class Packet:
                    id = 42

                return Packet()

        yield FakeIface()

    monkeypatch.setattr(admin, "connect", fake_connect)

    # Call send_text without specifying tx_linger_s
    admin.send_text(text="test", port="/dev/ttyUSB0")

    # Verify connect was called with the default 8.0s linger
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] == 8.0, (
        f"Expected default linger_s=8.0, got {connect_calls[0]}"
    )


def test_set_owner_uses_linger(monkeypatch):
    """set_owner() must use linger_s > 0 for flash write safety."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})

        class FakeNode:
            def setOwner(self, long_name, short_name):
                pass

        class FakeIface:
            localNode = FakeNode()

        yield FakeIface()

    monkeypatch.setattr(admin, "connect", fake_connect)

    # Call set_owner
    admin.set_owner(long_name="Test Owner", short_name="TEST")

    # Verify connect was called with a nonzero linger
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] > 0, (
        f"set_owner must use nonzero linger, got {connect_calls[0]['linger_s']}"
    )


def test_set_config_uses_linger(monkeypatch):
    """set_config() must use linger_s > 0 for flash write safety."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})
        # Immediately raise an error before set_config continues
        # (we just want to verify connect was called with linger)
        raise admin.AdminError("test error")

    monkeypatch.setattr(admin, "connect", fake_connect)

    # Call set_config — it should fail but we can check connect was called
    with pytest.raises(admin.AdminError):
        admin.set_config(path="lora.region", value="US")

    # Verify connect was called with a nonzero linger
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] > 0, (
        f"set_config must use nonzero linger, got {connect_calls[0]['linger_s']}"
    )


def test_set_channel_url_uses_linger(monkeypatch):
    """set_channel_url() must use linger_s > 0 for flash write safety."""
    connect_calls = []

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        connect_calls.append({"port": port, "linger_s": linger_s})

        class FakeNode:
            channels = []

            def setURL(self, url):
                pass

        class FakeIface:
            localNode = FakeNode()

        yield FakeIface()

    monkeypatch.setattr(admin, "connect", fake_connect)

    # Call set_channel_url
    admin.set_channel_url(url="https://example.com/meshtastic/config")

    # Verify connect was called with a nonzero linger
    assert len(connect_calls) == 1
    assert connect_calls[0]["linger_s"] > 0, (
        f"set_channel_url must use nonzero linger, got {connect_calls[0]['linger_s']}"
    )


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
