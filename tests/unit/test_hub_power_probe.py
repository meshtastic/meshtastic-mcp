# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Hub-flag power detection: `_power.wait_for_absence(resolved=...)`,
`_power.hub_cuts_power`, and `uhubctl.device_on_port`.

The bench lesson these pin: after a real VBUS cut, macOS retains a zombie of
the powered-off device in `ioreg`/`system_profiler`/`/dev` for an unbounded
time, so absence must be read from the HUB's own connect flag, not OS
enumeration. All hub calls are monkeypatched — no hardware needed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("meshtastic")

from meshtastic_mcp import uhubctl
from tests import _power


def test_device_on_port_reads_hub_connect_flag(monkeypatch):
    hubs = [
        {
            "location": "20-3",
            "ports": [
                {"port": 7, "device_vid": 0x239A},  # device attached
                {"port": 5, "device_vid": None},  # port off, no device
            ],
        }
    ]
    monkeypatch.setattr(uhubctl, "list_hubs", lambda: hubs)
    assert uhubctl.device_on_port("20-3", 7) is True
    assert uhubctl.device_on_port("20-3", 5) is False
    assert uhubctl.device_on_port("20-3", 3) is False  # unknown port
    assert uhubctl.device_on_port("99-9", 7) is False  # unknown hub


def test_wait_for_absence_returns_when_hub_drops_device(monkeypatch):
    """Absence via the hub flag — ignores OS enumeration entirely."""
    seq = iter([True, True, False])  # present, present, then gone from the port
    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", lambda loc, p: next(seq))
    monkeypatch.setattr(_power.time, "sleep", lambda s: None)
    _power.wait_for_absence("rak4631", timeout_s=5.0, resolved=("20-3", 7))  # must not raise


def test_wait_for_absence_times_out_if_hub_keeps_device(monkeypatch):
    """A hub that never drops the device → TimeoutError (a truly non-switching hub)."""
    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", lambda loc, p: True)
    monkeypatch.setattr(_power.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        _power.wait_for_absence("rak4631", timeout_s=0.05, resolved=("20-3", 7))


@pytest.fixture()
def rig(monkeypatch):
    calls: dict = {"off": [], "on": [], "resolve": 0}
    monkeypatch.setattr(
        _power.uhubctl_mod,
        "resolve_target",
        lambda role: calls.__setitem__("resolve", calls["resolve"] + 1) or ("20-3", 7),
    )
    monkeypatch.setattr(
        _power.uhubctl_mod, "power_off", lambda loc, p: calls["off"].append((loc, p)) or {}
    )
    monkeypatch.setattr(
        _power.uhubctl_mod, "power_on", lambda loc, p: calls["on"].append((loc, p)) or {}
    )
    monkeypatch.setattr(_power.time, "sleep", lambda s: None)
    return calls


def test_hub_cuts_power_true_when_hub_drops_device(rig, monkeypatch):
    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", lambda loc, p: False)
    assert _power.hub_cuts_power("rak4631", absence_timeout_s=0.2) is True
    assert rig["off"] == [("20-3", 7)]
    assert rig["on"] == [("20-3", 7)]  # power restored


def test_hub_cuts_power_false_when_device_survives(rig, monkeypatch):
    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", lambda loc, p: True)
    assert _power.hub_cuts_power("rak4631", absence_timeout_s=0.05) is False
    assert rig["on"] == [("20-3", 7)]  # restored even on a non-switching hub


def test_hub_cuts_power_resolves_target_once_up_front(rig, monkeypatch):
    """resolve_target runs once, before the cut — a powered-off device is
    invisible to it, so resolving during restore would raise."""
    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", lambda loc, p: False)
    _power.hub_cuts_power("rak4631")
    assert rig["resolve"] == 1


def test_hub_cuts_power_restores_power_when_probe_raises(rig, monkeypatch):
    def boom(loc, p):
        raise RuntimeError("uhubctl exploded")

    monkeypatch.setattr(_power.uhubctl_mod, "device_on_port", boom)
    with pytest.raises(RuntimeError):
        _power.hub_cuts_power("rak4631")
    assert rig["on"] == [("20-3", 7)]  # finally-block restore
