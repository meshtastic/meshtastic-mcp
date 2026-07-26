# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The bake's nRF52 DFU-entry must retry the 1200bps touch across multiple
rounds with a power-cycle of the board's own hub slot between rounds.

Regression for the bench T114 (HT-n5262): its app-mode USB stack ignored the
touch two nights running, but entered DFU immediately after a power-cycle.
All hardware calls are monkeypatched — no boards or hub needed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("meshtastic")

from tests import test_00_bake as bake


def _patch_common(monkeypatch, hub="20-3", slot=2):
    monkeypatch.setattr(bake.port_recovery, "hub_slot_for_port", lambda p: (hub, slot))
    monkeypatch.setattr(bake.port_recovery, "port_on_slot", lambda h, s: "/dev/cu.usbmodemAPP")
    monkeypatch.setattr(bake.time, "sleep", lambda s: None)
    monkeypatch.setattr(bake, "_DFU_REENUM_TIMEOUT_S", 0.1)


def test_touch_failure_power_cycles_then_succeeds(monkeypatch):
    """Round 1 fails → slot power-cycled → round 2 succeeds. The cycle hits
    the board's OWN slot, and the touch resumes on the re-resolved port."""
    _patch_common(monkeypatch)
    calls = {"touch": 0, "cycles": []}

    def fake_touch(port, settle_ms, retries):
        calls["touch"] += 1
        if calls["touch"] == 1:
            return {"ok": False, "attempts": retries}
        return {"ok": True, "new_port": "/dev/cu.usbmodemDFU"}

    monkeypatch.setattr(bake.flash, "touch_1200bps", fake_touch)
    monkeypatch.setattr(
        bake.uhubctl, "cycle", lambda loc, p, delay_s: calls["cycles"].append((loc, p))
    )

    result = bake._prepare_nrf52_for_upload("/dev/cu.usbmodem143201")
    assert calls["touch"] == 2
    assert calls["cycles"] == [("20-3", 2)]  # exactly one cycle, on this board's slot
    # After success the helper re-pins to whatever sits on THIS slot.
    assert result == "/dev/cu.usbmodemAPP"


def test_all_rounds_exhausted_raises_with_round_count(monkeypatch):
    """Every round fails → AssertionError mentioning the round count; a cycle
    ran between every pair of rounds (rounds-1 cycles), never after the last."""
    _patch_common(monkeypatch)
    calls = {"touch": 0, "cycles": 0}

    monkeypatch.setattr(
        bake.flash,
        "touch_1200bps",
        lambda port, settle_ms, retries: (
            calls.__setitem__("touch", calls["touch"] + 1) or {"ok": False, "attempts": retries}
        ),
    )
    monkeypatch.setattr(
        bake.uhubctl,
        "cycle",
        lambda loc, p, delay_s: calls.__setitem__("cycles", calls["cycles"] + 1),
    )

    with pytest.raises(AssertionError, match="touch rounds"):
        bake._prepare_nrf52_for_upload("/dev/cu.usbmodem143201")
    assert calls["touch"] == bake._DFU_TOUCH_ROUNDS
    assert calls["cycles"] == bake._DFU_TOUCH_ROUNDS - 1


def test_no_hub_slot_still_retries_without_cycling(monkeypatch):
    """A board with no resolvable hub slot degrades to plain re-touches."""
    _patch_common(monkeypatch, hub=None, slot=None)
    calls = {"touch": 0, "cycles": 0}

    def fake_touch(port, settle_ms, retries):
        calls["touch"] += 1
        if calls["touch"] < 3:
            return {"ok": False, "attempts": retries}
        return {"ok": True, "new_port": "/dev/cu.usbmodemDFU"}

    monkeypatch.setattr(bake.flash, "touch_1200bps", fake_touch)
    monkeypatch.setattr(
        bake.uhubctl,
        "cycle",
        lambda loc, p, delay_s: calls.__setitem__("cycles", calls["cycles"] + 1),
    )

    result = bake._prepare_nrf52_for_upload("/dev/cu.usbmodem143201")
    assert calls["touch"] == 3
    assert calls["cycles"] == 0  # no slot → no cycling, but retries continue
    assert result == "/dev/cu.usbmodemDFU"  # no slot re-pin either


def test_cycle_failure_degrades_to_plain_retouch(monkeypatch):
    """uhubctl erroring between rounds must not sink the bake — the next
    round proceeds as a plain re-touch on the original port."""
    _patch_common(monkeypatch)
    calls = {"touch": 0}

    def fake_touch(port, settle_ms, retries):
        calls["touch"] += 1
        if calls["touch"] == 1:
            return {"ok": False, "attempts": retries}
        return {"ok": True, "new_port": "/dev/cu.usbmodemDFU"}

    monkeypatch.setattr(bake.flash, "touch_1200bps", fake_touch)

    def boom(loc, p, delay_s):
        raise RuntimeError("uhubctl not present")

    monkeypatch.setattr(bake.uhubctl, "cycle", boom)

    result = bake._prepare_nrf52_for_upload("/dev/cu.usbmodem143201")
    assert calls["touch"] == 2
    assert result == "/dev/cu.usbmodemAPP"
