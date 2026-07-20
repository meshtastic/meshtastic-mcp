# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`_power.hub_cuts_power` — the once-per-tier probe that detects hubs which
only pretend to switch power (status bits flip, VBUS stays hot).

Regression for the reference bench's Terminus FE 2.1 clone (1a40:0201):
uhubctl reported every port `off` while the device node never left /dev, so
the whole peer-offline tier failed with "didn't disappear after power_off".
All hub/USB calls are monkeypatched — no hardware needed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("meshtastic")

from tests import _power


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
    return calls


def test_true_when_device_deenumerates(rig, monkeypatch):
    monkeypatch.setattr(_power, "wait_for_absence", lambda role, timeout_s, expected_port: None)
    assert _power.hub_cuts_power("rak4631", expected_port="/dev/x") is True
    assert rig["off"] == [("20-3", 7)]
    assert rig["on"] == [("20-3", 7)]  # power restored


def test_false_when_device_survives_the_cut(rig, monkeypatch):
    def never_absent(role, timeout_s, expected_port):
        raise TimeoutError("still enumerated")

    monkeypatch.setattr(_power, "wait_for_absence", never_absent)
    assert _power.hub_cuts_power("rak4631", expected_port="/dev/x") is False
    assert rig["on"] == [("20-3", 7)]  # power restored even on a no-op hub


def test_resolves_target_once_before_the_cut(rig, monkeypatch):
    """resolve_target must run exactly once, up-front — a powered-off device
    is invisible to it, so resolving during restore would raise."""
    monkeypatch.setattr(_power, "wait_for_absence", lambda role, timeout_s, expected_port: None)
    _power.hub_cuts_power("rak4631")
    assert rig["resolve"] == 1


def test_power_restored_when_wait_raises_unexpectedly(rig, monkeypatch):
    def boom(role, timeout_s, expected_port):
        raise RuntimeError("list_devices exploded")

    monkeypatch.setattr(_power, "wait_for_absence", boom)
    with pytest.raises(RuntimeError):
        _power.hub_cuts_power("rak4631")
    assert rig["on"] == [("20-3", 7)]  # finally-block restore
