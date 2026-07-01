# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Correctness of `rf_oracle`'s window-matching helpers — no hardware needed.

Regression test for a real bug found via live-hardware testing: a high-gain
RTL-SDR capture produced ~40 sub-millisecond noise spikes alongside one
genuine ~1s LoRa burst, and naive nearest-by-time matching picked a noise
spike (closer in raw time to the send offset) over the real, much longer
burst. `_min_plausible_burst_s` + `_closest_window`'s `min_duration_s` filter
fixes this by rejecting anything shorter than a LoRa preamble could be before
considering a window a candidate match.
"""

from __future__ import annotations

from meshtastic_mcp import rf_oracle
from meshtastic_mcp.sdr import ActiveWindow


def test_min_plausible_burst_s_scales_with_symbol_time() -> None:
    # SF11/250kHz (LONG_FAST): symbol time = 2^11/250000 = 8.192ms; 8 symbols ~= 65.5ms.
    assert rf_oracle._min_plausible_burst_s(sf=11, bw_khz=250.0) > 0.06
    # Higher SF (slower, e.g. LONG_SLOW SF12) means longer symbols, so a higher floor.
    slow = rf_oracle._min_plausible_burst_s(sf=12, bw_khz=125.0)
    fast = rf_oracle._min_plausible_burst_s(sf=7, bw_khz=500.0)
    assert slow > fast


def test_closest_window_ignores_noise_spikes_shorter_than_min_duration() -> None:
    # Reproduces the exact real-world shape: many ~0.125ms noise spikes plus one
    # genuine ~1s burst about 1.17s after the (simulated) send offset — closer in
    # time is a spike, but only the long burst is a plausible LoRa packet.
    windows = [
        ActiveWindow(0.000875, 0.001),
        ActiveWindow(0.128875, 0.129),
        ActiveWindow(0.256875, 0.257),  # nearest by raw time to offset_s=0.2163
        ActiveWindow(1.3875, 2.375),  # the real ~1s burst
    ]
    offset_s = 0.2163
    min_duration_s = rf_oracle._min_plausible_burst_s(sf=11, bw_khz=250.0)

    naive = rf_oracle._closest_window(windows, offset_s, max_distance_s=3.0)
    assert naive is not None
    assert naive.start_s == 0.256875, "sanity check: naive nearest-by-time picks the noise spike"

    filtered = rf_oracle._closest_window(
        windows, offset_s, max_distance_s=3.0, min_duration_s=min_duration_s
    )
    assert filtered is not None
    assert filtered.start_s == 1.3875, "with the noise filter, the real burst should win instead"


def test_closest_window_returns_none_when_only_noise_present() -> None:
    windows = [ActiveWindow(0.1, 0.1001), ActiveWindow(0.2, 0.2001)]
    result = rf_oracle._closest_window(windows, 0.15, max_distance_s=3.0, min_duration_s=0.05)
    assert result is None


def test_closest_window_respects_max_distance_even_with_duration_filter() -> None:
    # A plausible-duration window that's just too far away from the send should
    # still be rejected — min_duration_s narrows candidates, it doesn't widen tolerance.
    windows = [ActiveWindow(10.0, 11.0)]
    result = rf_oracle._closest_window(windows, 0.2, max_distance_s=3.0, min_duration_s=0.05)
    assert result is None
