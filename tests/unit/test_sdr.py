# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure-numpy analysis correctness for `sdr.py`, against synthetic IQ.

No RTL-SDR hardware needed — every function here operates on an in-memory
ndarray, so we can build a known signal (a tone, or a tone burst with known
on/off timing) and assert the analysis recovers its known parameters. This is
the regression net for the compliance oracle's measurement half; `lora_compliance`
tests are the prediction half.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")  # optional extra — a bare [test] install skips these

import numpy as np

from meshtastic_mcp import sdr


def _tone(
    freq_hz: float, sample_rate_hz: float, duration_s: float, amplitude: float = 1.0
) -> np.ndarray:
    n = int(duration_s * sample_rate_hz)
    t = np.arange(n) / sample_rate_hz
    return (amplitude * np.exp(2j * np.pi * freq_hz * t)).astype(np.complex64)


def _noise(n: int, amplitude: float = 0.01, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amplitude * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def test_power_spectrum_peak_at_tone_frequency() -> None:
    sample_rate = 2_000_000.0
    tone_freq = 250_000.0  # offset from "center" (0 Hz in this relative-frequency convention)
    iq = _tone(tone_freq, sample_rate, 0.01) + _noise(int(0.01 * sample_rate))

    freqs, psd_db = sdr.power_spectrum_db(iq, sample_rate, nfft=2048)
    peak_freq = freqs[int(np.argmax(psd_db))]
    # nfft=2048 @ 2Msps -> ~977 Hz/bin resolution; allow a couple bins of slack.
    assert abs(peak_freq - tone_freq) < 5 * (sample_rate / 2048)


def test_occupied_bandwidth_narrow_for_pure_tone() -> None:
    sample_rate = 2_000_000.0
    iq = _tone(0.0, sample_rate, 0.01) + _noise(int(0.01 * sample_rate))
    ob = sdr.occupied_bandwidth(iq, sample_rate, db_down=20.0, nfft=2048)
    # A pure tone's energy should be tightly concentrated — well under 1% of the sample rate.
    assert ob.bw_hz < sample_rate * 0.01
    assert abs(ob.peak_freq_hz) < 5 * (sample_rate / 2048)


def test_occupied_bandwidth_wider_for_wideband_signal() -> None:
    sample_rate = 2_000_000.0
    n = int(0.01 * sample_rate)
    # A wideband chirp-ish signal (linear FM sweep) occupies much more spectrum than a tone.
    t = np.arange(n) / sample_rate
    sweep_bw = 500_000.0
    chirp = np.exp(2j * np.pi * (-sweep_bw / 2 * t + (sweep_bw / (2 * t[-1])) * t**2)).astype(
        np.complex64
    )
    iq = chirp + _noise(n)

    ob_tone = sdr.occupied_bandwidth(
        _tone(0.0, sample_rate, 0.01) + _noise(n), sample_rate, nfft=2048
    )
    ob_chirp = sdr.occupied_bandwidth(iq, sample_rate, nfft=2048)
    assert ob_chirp.bw_hz > ob_tone.bw_hz * 10


def test_in_band_fraction_high_when_tone_inside_band() -> None:
    sample_rate = 2_000_000.0
    capture_center = 915_000_000.0
    tone_offset = 100_000.0  # 915.1 MHz absolute
    iq = _tone(tone_offset, sample_rate, 0.01) + _noise(int(0.01 * sample_rate))

    frac_in = sdr.in_band_fraction(
        iq,
        sample_rate,
        capture_center_hz=capture_center,
        band_start_hz=902_000_000.0,
        band_end_hz=928_000_000.0,
    )
    assert frac_in > 0.95


def test_in_band_fraction_low_when_tone_outside_band() -> None:
    sample_rate = 2_000_000.0
    capture_center = 915_000_000.0
    tone_offset = 800_000.0  # 915.8 MHz absolute — outside a narrow allowed band below
    iq = _tone(tone_offset, sample_rate, 0.01) + _noise(int(0.01 * sample_rate))

    frac_in = sdr.in_band_fraction(
        iq,
        sample_rate,
        capture_center_hz=capture_center,
        band_start_hz=914_000_000.0,
        band_end_hz=915_200_000.0,  # excludes the 915.8MHz tone
    )
    assert frac_in < 0.3


def test_active_windows_detects_burst_timing() -> None:
    sample_rate = 1_000_000.0
    # 20ms silence, 5ms tone burst, 20ms silence — burst is a minority of windows so
    # the median-based noise floor lands on the silence level, not the burst.
    silence_a = _noise(int(0.020 * sample_rate), amplitude=0.005)
    burst = _tone(50_000.0, sample_rate, 0.005, amplitude=1.0)
    silence_b = _noise(int(0.020 * sample_rate), amplitude=0.005)
    iq = np.concatenate([silence_a, burst, silence_b])

    windows = sdr.active_windows(iq, sample_rate, threshold_db=15.0, win_samples=64)
    assert len(windows) == 1
    w = windows[0]
    assert abs(w.start_s - 0.020) < 0.001
    assert abs(w.duration_s - 0.005) < 0.001


def test_duty_cycle_pct_matches_active_fraction() -> None:
    windows = [sdr.ActiveWindow(0.0, 1.0), sdr.ActiveWindow(2.0, 2.5)]
    assert sdr.duty_cycle_pct(windows, observation_window_s=10.0) == pytest.approx(15.0)


def test_duty_cycle_pct_zero_for_empty_observation() -> None:
    assert sdr.duty_cycle_pct([], observation_window_s=0.0) == 0.0


def test_snap_sample_rate_passes_through_valid_values() -> None:
    assert sdr.snap_sample_rate(250_000.0) == 250_000.0
    assert sdr.snap_sample_rate(2_048_000.0) == 2_048_000.0


def test_snap_sample_rate_avoids_the_forbidden_gap() -> None:
    # 750kHz (e.g. a naive span_khz*1.5 calc for a 500kHz span) falls in the RTL2832U's
    # unsupported [300k, 900k) gap and must be snapped up to the next valid tier's floor.
    assert sdr.snap_sample_rate(750_000.0) == 900_001.0


def test_snap_sample_rate_clamps_below_and_above_range() -> None:
    assert sdr.snap_sample_rate(1_000.0) == 225_001.0
    assert sdr.snap_sample_rate(10_000_000.0) == 3_200_000.0


def test_require_rtlsdr_raises_sdr_error_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "rtlsdr":
            raise ImportError("no module named rtlsdr")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(sdr.SdrError, match="pyrtlsdr is not installed"):
        sdr.list_devices()
