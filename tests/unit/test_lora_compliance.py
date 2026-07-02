# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Correctness of the firmware region/preset → RF-parameter port.

No hardware, no firmware checkout needed — these pin the pure-Python port in
`lora_compliance.py` against hand-derived values from the firmware source it
mirrors (see that module's docstring for file:line citations), so a future
re-sync with upstream has a regression net.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp import lora_compliance as lc


def test_djb2_hash_matches_known_vectors() -> None:
    # djb2 with the firmware's exact recurrence (hash*33 + c, 32-bit wraparound).
    # Hand-computed for short, easily-verified strings.
    assert lc.djb2_hash("") == 5381
    assert lc.djb2_hash("a") == 5381 * 33 + ord("a")
    # "LongFast" — the default channel name hashed for the default US LongFast slot.
    h = 5381
    for ch in "LongFast":
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    assert lc.djb2_hash("LongFast") == h


def test_us_default_long_fast_in_band_and_250khz() -> None:
    pred = lc.predict_lora_params("US", "LONG_FAST")
    assert 902.0 <= pred.freq_mhz <= 928.0
    assert pred.bw_khz == 250.0
    assert pred.sf == 11
    assert pred.cr == 5
    assert pred.duty_cycle_pct == 100
    assert pred.power_limit_dbm == 30
    assert 0 <= pred.channel_num < pred.num_freq_slots


def test_eu_868_narrow_band_and_duty_cycle() -> None:
    pred = lc.predict_lora_params("EU_868", "LONG_FAST")
    assert 869.4 <= pred.freq_mhz <= 869.65
    assert pred.duty_cycle_pct == 10
    assert pred.power_limit_dbm == 27


def test_eu_866_duty_cycle_is_role_dependent() -> None:
    mobile = lc.predict_lora_params("EU_866", "LITE_FAST", device_role="CLIENT")
    router = lc.predict_lora_params("EU_866", "LITE_FAST", device_role="ROUTER")
    assert mobile.duty_cycle_pct == 2.5
    assert router.duty_cycle_pct == 10.0


def test_override_frequency_wins_outright() -> None:
    pred = lc.predict_lora_params(
        "US", "LONG_FAST", override_frequency_mhz=906.875, frequency_offset_mhz=0.5
    )
    assert pred.freq_mhz == pytest.approx(907.375)
    assert pred.channel_num == -1


def test_frequency_offset_is_additive() -> None:
    base = lc.predict_lora_params("US", "LONG_FAST", channel_num=1)
    offset = lc.predict_lora_params("US", "LONG_FAST", channel_num=1, frequency_offset_mhz=1.0)
    assert offset.freq_mhz == pytest.approx(base.freq_mhz + 1.0)


def test_explicit_channel_num_resolves_slot_deterministically() -> None:
    # channel_num is 1-based on the wire; slot 1 -> resolved channel_num 0 -> band edge + half BW.
    pred = lc.predict_lora_params("US", "LONG_FAST", channel_num=1)
    assert pred.channel_num == 0
    assert pred.freq_mhz == pytest.approx(902.0 + 250.0 / 2000.0)


def test_wide_lora_region_uses_wide_bandwidth_table() -> None:
    pred = lc.predict_lora_params("LORA_24", "LONG_FAST")
    assert pred.wide_lora is True
    assert pred.bw_khz == 812.5  # wideLora variant, not the 250kHz sub-GHz figure
    assert 2400.0 <= pred.freq_mhz <= 2483.5


def test_ham_region_explicit_override_slot() -> None:
    # ITU1_2M hardcodes slot 26 regardless of channel name hash.
    pred = lc.predict_lora_params("ITU1_2M", "TINY_FAST")
    assert pred.channel_num == 25  # 1-based 26 -> 0-based 25


def test_unknown_region_raises() -> None:
    with pytest.raises(ValueError):
        lc.predict_lora_params("ATLANTIS", "LONG_FAST")


def test_unknown_preset_raises() -> None:
    with pytest.raises(ValueError):
        lc.predict_lora_params("US", "ULTRA_FAST")


def test_effective_power_limit_clamps_to_region() -> None:
    assert lc.effective_power_limit_dbm("US", configured_tx_power_dbm=99) == 30
    assert lc.effective_power_limit_dbm("US", configured_tx_power_dbm=10) == 10
    assert lc.effective_power_limit_dbm("US", configured_tx_power_dbm=0) == 30


def test_effective_power_limit_licensed_bypasses_clamp() -> None:
    assert lc.effective_power_limit_dbm("US", configured_tx_power_dbm=33, is_licensed=True) == 33


@pytest.mark.parametrize("region", list(lc.REGIONS.keys()))
def test_every_region_default_preset_predicts_in_band(region: str) -> None:
    """Every region's own default preset should resolve to a frequency inside
    [freq_start, freq_end] — a basic self-consistency check across the whole
    table (catches transcription typos like a swapped start/end)."""
    info = lc.REGIONS[region]
    pred = lc.predict_lora_params(region, info.default_preset)
    assert info.freq_start_mhz <= pred.freq_mhz <= info.freq_end_mhz, (
        f"{region}/{info.default_preset}: predicted {pred.freq_mhz} MHz outside "
        f"[{info.freq_start_mhz}, {info.freq_end_mhz}]"
    )
