# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""nRF52 power-cycle reconnect hardening.

Two guards pinned here, both from run-32 failures rooted in nRF52 native-USB
CDC fragility across a VBUS cut:

* ``resolve_port_by_role(require_openable=True)`` must not return a path that
  ``list_devices`` reports but that fails to open — the flapping re-enumeration
  that made ``esp32s3->t_echo`` fail with ``could not open port``.
* ``_power.recover_absent_role`` must swallow a failed wake (empty slot / no
  hub) and yield None, never raise — the session ``bench_wake`` depends on it
  to leave a genuinely-absent board absent instead of erroring the run.

All hardware is monkeypatched — no bench needed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("meshtastic")

import meshtastic_mcp.port_recovery as pr
import tests._port_discovery as pd
from tests import _power


def _one_esp32s3(monkeypatch) -> None:
    """A single esp32s3 device on the bus, matched by VID (no location pin)."""
    monkeypatch.setattr(pd, "_role_location", lambda role: None)
    monkeypatch.setattr(
        pd.devices_module,
        "list_devices",
        lambda include_unknown=True: [{"port": "/dev/cu.fake", "vid": "0x10c4"}],
    )
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)


def test_require_openable_returns_a_settled_port(monkeypatch):
    _one_esp32s3(monkeypatch)
    monkeypatch.setattr(pr, "port_openable", lambda port, exclusive=True, timeout=1.0: (True, None))
    assert (
        pd.resolve_port_by_role("esp32s3", require_openable=True, timeout_s=5.0) == "/dev/cu.fake"
    )


def test_require_openable_waits_out_a_flapping_path(monkeypatch):
    """A path that lists but fails its first open is skipped; polling continues
    until the CDC settles (opens twice)."""
    _one_esp32s3(monkeypatch)
    # round 1: first open False -> not settled -> keep polling.
    # round 2: True, then True -> settled -> return.
    seq = iter([False, True, True])
    monkeypatch.setattr(
        pr, "port_openable", lambda port, exclusive=True, timeout=1.0: (next(seq), None)
    )
    assert (
        pd.resolve_port_by_role("esp32s3", require_openable=True, timeout_s=5.0) == "/dev/cu.fake"
    )


def test_require_openable_times_out_when_never_openable(monkeypatch):
    """A path that lists but never opens (permanently wedged) must raise, not
    return a doomed port."""
    _one_esp32s3(monkeypatch)
    monkeypatch.setattr(
        pr, "port_openable", lambda port, exclusive=True, timeout=1.0: (False, None)
    )
    with pytest.raises(AssertionError):
        pd.resolve_port_by_role("esp32s3", require_openable=True, timeout_s=0.05)


def test_default_ignores_openability(monkeypatch):
    """Back-compat: without require_openable a matched path returns immediately,
    never touching port_openable."""
    _one_esp32s3(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("port_openable must not be called in the default path")

    monkeypatch.setattr(pr, "port_openable", boom)
    assert pd.resolve_port_by_role("esp32s3", timeout_s=5.0) == "/dev/cu.fake"


def test_recover_absent_role_returns_port_on_success(monkeypatch):
    monkeypatch.setattr(
        _power, "power_cycle", lambda role, rediscover_timeout_s=25.0: "/dev/cu.woke"
    )
    assert _power.recover_absent_role("heltec_t114") == "/dev/cu.woke"


def test_recover_absent_role_swallows_failure(monkeypatch):
    """An empty slot makes power_cycle's resolve raise; recover must return None
    so bench_wake treats the board as genuinely absent."""

    def boom(role, rediscover_timeout_s=25.0):
        raise AssertionError("no device matching role appeared")

    monkeypatch.setattr(_power, "power_cycle", boom)
    assert _power.recover_absent_role("heltec_t114") is None
