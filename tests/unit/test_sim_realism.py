# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""WS5 realism regression: keep the generator inside tolerance bands.

These bands are reviewed constants derived from the real event captures
(Burning Man 2025, DEF CON 33) analysed in docs/sim-realism-plan.md. The
golden stat profiles themselves are dataset-derived and not committed, so the
guardrails live here as code — they catch regressions in the generator's
portnum economy, hop model, text shape, telemetry, and the RF observer without
needing the private data present.

Scale is kept modest (500 nodes / 2 days) so the suite stays fast; the bands
are shape/proportion based and hold across scales.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp.replay import metrics, sim

SEED = 99
START = 1_700_000_000


def _stats(profile):
    cap = sim.generate(nodes=500, days=2, seed=SEED, start=START, profile=profile)
    return cap, metrics.capture_stats(cap)


def _mix_pct(stats):
    total = sum(stats["portnum_mix"].values()) or 1
    return {k: 100.0 * v / total for k, v in stats["portnum_mix"].items()}


@pytest.fixture(scope="module")
def preset_stats():
    return {name: _stats(name) for name in ("meshcon", "burningman", "defcon")}


@pytest.mark.parametrize("preset", ["meshcon", "burningman", "defcon"])
def test_portnum_economy_within_bands(preset, preset_stats):
    """Real meshes are NODEINFO/POSITION-dominated with a real ROUTING share —
    the inverse of the pre-calibration generator (which was TELEMETRY-heavy)."""
    _cap, stats = preset_stats[preset]
    mix = _mix_pct(stats)
    assert 25.0 <= mix.get("NODEINFO", 0) <= 45.0, mix
    assert 22.0 <= mix.get("POSITION", 0) <= 38.0, mix
    assert 8.0 <= mix.get("TELEMETRY", 0) <= 28.0, mix
    assert 3.0 <= mix.get("ROUTING", 0) <= 15.0, mix
    # NODEINFO must lead TELEMETRY (the key inversion the calibration fixed)
    assert mix["NODEINFO"] > mix["TELEMETRY"]


@pytest.mark.parametrize(
    ("preset", "lo", "hi"),
    [("meshcon", 0.15, 0.35), ("burningman", 0.05, 0.25), ("defcon", 0.35, 0.55)],
)
def test_encrypted_fraction_per_scenario(preset, lo, hi, preset_stats):
    _cap, stats = preset_stats[preset]
    assert lo <= stats["encrypted_fraction"] <= hi


def test_text_shape(preset_stats):
    """Short median with a long wall-of-text tail (real p50 17-25, max 227-246)."""
    for preset in ("meshcon", "burningman", "defcon"):
        _cap, stats = preset_stats[preset]
        length = stats["text"]["len"]
        assert 15 <= length["p50"] <= 40, (preset, length)
        assert length["max"] >= 150, (preset, length)
        assert stats["text"]["dm_fraction"] <= 0.10  # DMs are rare on-air


def test_talker_skew_and_hop_model(preset_stats):
    for preset in ("meshcon", "burningman", "defcon"):
        _cap, stats = preset_stats[preset]
        assert 0.30 <= stats["talker_skew"]["top10pct_share"] <= 0.85
        assert "7" in stats["hop_start"]  # the observed hop-7 subpopulation exists

    # DEF CON cranks hop-7 harder than the default MeshCon config
    def hop7_frac(stats):
        hs = {int(k): v for k, v in stats["hop_start"].items()}
        return hs.get(7, 0) / max(sum(hs.values()), 1)

    assert hop7_frac(preset_stats["defcon"][1]) > hop7_frac(preset_stats["meshcon"][1])


def test_telemetry_realism(preset_stats):
    for preset in ("meshcon", "burningman", "defcon"):
        _cap, stats = preset_stats[preset]
        tel = stats["telemetry"]
        # chutil skewed low with a bounded tail (BM p50 7.4, max 39)
        assert 3.0 <= tel["chutil"]["p50"] <= 20.0, (preset, tel["chutil"])
        assert tel["chutil"]["max"] < 55.0
        # battery is bimodal: a plugged (101) mode AND a drained (0) spike
        batt = dict(tel["battery_top"])
        assert 101 in batt
        assert 0 in batt
        # device metrics dominate, env + power present
        vm = tel["variant_mix"]
        assert vm.get("device_metrics", 0) > vm.get("environment_metrics", 0)
        assert vm.get("environment_metrics", 0) > 0


def test_observer_presets_produce_gateway_view(preset_stats):
    """burningman/defcon enable the RF observer: RX metadata + rebroadcast
    duplicates. meshcon leaves it off (omniscient truth)."""
    for preset in ("burningman", "defcon"):
        _cap, stats = preset_stats[preset]
        assert stats["rx"]["rssi"]["n"] > 0
        assert -128 <= stats["rx"]["rssi"]["p50"] <= -12
        dup2 = sum(int(v) for k, v in stats["dup_id_multiplicity"].items() if k != "1")
        assert dup2 > 0, "observer should duplicate rebroadcast packets"

    _cap, mesh = preset_stats["meshcon"]
    assert mesh["rx"]["rssi"]["n"] == 0  # observer off by default


def test_generator_is_byte_deterministic():
    a = sim.generate(nodes=200, days=1, seed=5, start=START, profile="defcon")
    b = sim.generate(nodes=200, days=1, seed=5, start=START, profile="defcon")
    assert a.packets == b.packets


def test_presets_are_tak_free_by_default(preset_stats):
    for preset in ("meshcon", "burningman", "defcon"):
        _cap, stats = preset_stats[preset]
        assert stats["tak_packets"] == 0
