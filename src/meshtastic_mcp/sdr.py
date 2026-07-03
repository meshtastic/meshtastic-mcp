# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""RTL-SDR capture + spectral analysis — the measurement half of the RF
compliance oracle (`lora_compliance.py` is the prediction half).

Deliberately does **not** attempt LoRa chirp demodulation — that's a hard,
narrow problem other tools (meshtastic-sniffer, gr-lora_sdr, MeshRF, the
SDRangel Meshtastic plugins) already solve well. What we need for compliance
testing is coarser and more tractable: where is the energy, how wide is it,
how long is it on, and does that match what `lora_compliance.predict_lora_params`
says the device should be doing. All analysis below operates on raw IQ with
plain `numpy` — no SDR access needed to unit test it (see
`tests/unit/test_sdr.py`, which feeds synthetic IQ).

Hardware access is via `pyrtlsdr` (the `sdr` extra: `pip install
'meshtastic-mcp[sdr]'`), which wraps `librtlsdr` directly — chosen over
shelling out to `rtl_power`/`rtl_sdr` so capture timing can be controlled
precisely (start capturing *before* triggering `send_text()`, stop right
after) rather than parsing another process's periodic text output.

Caveat that matters: a ~$25 RTL-SDR has no calibrated power reference, no
oven-stabilized clock, and a noisy front end. Treat every measurement here as
a **dev-loop regression check** ("did this change shift behavior"), not a
certified compliance measurement for regulatory sign-off — that requires a
calibrated EMC lab.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy ships with the [sdr] extra — keep the base install slim
    import numpy as np


class SdrError(RuntimeError):
    """Raised for missing `pyrtlsdr`/`librtlsdr`, no device, or capture failure."""


def _require_rtlsdr():
    try:
        from rtlsdr import RtlSdr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SdrError(
            "pyrtlsdr is not installed. Install the 'sdr' extra: pip install 'meshtastic-mcp[sdr]' "
            "(bundles pyrtlsdrlib, a prebuilt librtlsdr — no system package needed)."
        ) from exc
    except Exception as exc:  # e.g. ctypes AttributeError: wrong/old system librtlsdr
        raise SdrError(
            f"pyrtlsdr failed to bind librtlsdr: {exc}. "
            "A system librtlsdr (e.g. Homebrew's osmocom fork) may be the wrong ABI "
            "(missing rtlsdr_set_dithering). Install pyrtlsdrlib for a prebuilt "
            "librtlsdr that matches pyrtlsdr: `pip install pyrtlsdrlib`."
        ) from exc
    return RtlSdr


def list_devices() -> list[str]:
    """List serial numbers of attached RTL-SDR devices (does not open any of them)."""
    RtlSdr = _require_rtlsdr()
    try:
        return list(RtlSdr.get_device_serial_addresses())
    except Exception as exc:
        raise SdrError(f"Could not enumerate RTL-SDR devices: {exc}") from exc


# RTL2832U hardware sample-rate limits (librtlsdr's rtlsdr_set_sample_rate): valid ranges are
# roughly 225.001-300kHz and 900.001kHz-3.2Msps. Everything in [300k, 900k) is rejected by the
# tuner/ADC and raises a LibUSBError -22 ("invalid argument") if requested directly.
_RTLSDR_VALID_RATE_RANGES: tuple[tuple[float, float], ...] = (
    (225_001.0, 300_000.0),
    (900_001.0, 3_200_000.0),
)


def snap_sample_rate(hz: float) -> float:
    """Snap a requested sample rate to the nearest value the RTL2832U will accept.

    Below the lowest valid range -> floor of that range. Inside the forbidden
    300k-900k gap -> floor of the next (high) range. Above the highest valid
    range -> ceiling of that range. Already-valid values pass through unchanged.
    """
    for lo, hi in _RTLSDR_VALID_RATE_RANGES:
        if lo <= hz <= hi:
            return hz
    lowest_floor = _RTLSDR_VALID_RATE_RANGES[0][0]
    if hz < lowest_floor:
        return lowest_floor
    highest_floor, highest_ceiling = _RTLSDR_VALID_RATE_RANGES[-1]
    if hz < highest_floor:
        return highest_floor
    return highest_ceiling


def capture_iq(
    center_freq_hz: float,
    sample_rate_hz: float,
    duration_s: float,
    *,
    gain: float | str = "auto",
    device_index: int = 0,
    settle_s: float = 0.05,
    chunk_samples: int = 256 * 1024,
) -> np.ndarray:
    """Tune to `center_freq_hz` and capture `duration_s` seconds of IQ.

    Thin wrapper around `capture_iq_timed()` for callers that don't need
    precise wall-clock correlation against an external event (e.g. `rf_scan`).
    """
    iq, _t_start, _t_end, _actual_rate = capture_iq_timed(
        center_freq_hz,
        sample_rate_hz,
        duration_s,
        gain=gain,
        device_index=device_index,
        settle_s=settle_s,
        chunk_samples=chunk_samples,
    )
    return iq


def capture_iq_timed(
    center_freq_hz: float,
    sample_rate_hz: float,
    duration_s: float,
    *,
    gain: float | str = "auto",
    device_index: int = 0,
    settle_s: float = 0.05,
    chunk_samples: int = 256 * 1024,
) -> tuple[np.ndarray, float, float, float]:
    """Like `capture_iq`, but also returns `(iq, t_start, t_end, actual_sample_rate_hz)`.

    `t_start`/`t_end` are `time.monotonic()` timestamps bracketing the *timed*
    capture window (after the `settle_s` discard, which absorbs PLL-lock/AGC
    jitter). This is what makes precise correlation against an external event
    possible: a caller that records `time.monotonic()` right before
    triggering that event (e.g. `send_text()`) can compute
    `event_offset_s = t_event - t_start` and know exactly where in the
    returned IQ array the event should appear, instead of averaging the whole
    capture window and blurring together unrelated RF activity captured in
    the same window (see `rf_oracle.confirm_tx`, which uses this to isolate
    one transmission from ambient mesh traffic).

    `actual_sample_rate_hz` is read back from the hardware after configuring
    it — `sample_rate_hz` is silently snapped to a valid value
    (`snap_sample_rate`) before being applied, AND the RTL2832U's internal
    rational-divider PLL only hits *exact* rates at certain values, so the
    achieved rate can differ from what was requested either way. Use the
    returned rate (not the one you passed in) for any frequency-axis math on
    the returned samples.

    Returns a complex64 ndarray of `round(duration_s * actual_sample_rate_hz)`
    samples, normalized to [-1, 1] (pyrtlsdr's convention). Reads in
    `chunk_samples`-sized calls and concatenates — single huge
    `read_samples()` calls can exceed librtlsdr's internal transfer-size
    comfort zone on some platforms.
    """
    import numpy as np  # lazy: ships with the [sdr] extra

    RtlSdr = _require_rtlsdr()
    try:
        sdr = RtlSdr(device_index=device_index)
    except Exception as exc:
        raise SdrError(f"Could not open RTL-SDR device_index={device_index}: {exc}") from exc

    try:
        sdr.sample_rate = snap_sample_rate(sample_rate_hz)
        actual_rate = float(sdr.sample_rate)
        sdr.center_freq = center_freq_hz
        sdr.gain = gain

        if settle_s > 0:
            sdr.read_samples(int(settle_s * actual_rate))

        total_needed = round(duration_s * actual_rate)
        chunks: list[np.ndarray] = []
        remaining = total_needed
        t_start = time.monotonic()
        while remaining > 0:
            n = min(chunk_samples, remaining)
            chunks.append(np.asarray(sdr.read_samples(n), dtype=np.complex64))
            remaining -= n
        t_end = time.monotonic()
        iq = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return iq, t_start, t_end, actual_rate
    except SdrError:
        raise
    except Exception as exc:
        raise SdrError(f"RTL-SDR capture failed: {exc}") from exc
    finally:
        sdr.close()


# ---------------------------------------------------------------------------
# Pure-numpy analysis — no hardware required, fully unit-testable.
# ---------------------------------------------------------------------------


def power_spectrum_db(
    iq: np.ndarray, sample_rate_hz: float, nfft: int = 4096
) -> tuple[np.ndarray, np.ndarray]:
    """Welch-averaged power spectral density.

    Returns `(freqs_hz, psd_db)`, `freqs_hz` centered on 0 (i.e. relative to
    whatever the capture's `center_freq_hz` was), fftshift-ordered ascending.
    Averaging across non-overlapping `nfft`-sample segments trades time
    resolution for a less noisy spectral estimate — appropriate here since we
    care about where the energy sits, not catching a single symbol.
    """
    import numpy as np  # lazy: ships with the [sdr] extra

    n = len(iq)
    if n < nfft:
        # fall back to a smaller power-of-2 for short captures
        nfft = max(8, 1 << (n.bit_length() - 1))
    usable = (n // nfft) * nfft
    if usable == 0:
        raise SdrError(f"capture too short ({n} samples) for nfft={nfft}")
    segments = iq[:usable].reshape(-1, nfft)
    window = np.hanning(nfft)
    spectra = np.fft.fftshift(np.fft.fft(segments * window, axis=1), axes=1)
    psd = np.mean(np.abs(spectra) ** 2, axis=0)
    psd_db = 10.0 * np.log10(psd + 1e-20)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate_hz))
    return freqs, psd_db


@dataclass(frozen=True)
class OccupiedBandwidth:
    low_hz: float  # relative to capture center
    high_hz: float
    bw_hz: float
    peak_freq_hz: float  # relative to capture center — the freq_offset estimate


def occupied_bandwidth(
    iq: np.ndarray, sample_rate_hz: float, *, db_down: float = 26.0, nfft: int = 4096
) -> OccupiedBandwidth:
    """ "N dB down" occupied bandwidth — the regulatory-style method (commonly
    20-26 dB down is used for LoRa/CSS signals; FCC/ETSI occupied-bandwidth
    methods are conceptually the same idea applied with their own thresholds).

    Finds the peak bin, then walks outward in both directions until the PSD
    drops more than `db_down` below the peak, contiguously. This is a coarse
    bandwidth estimate, not a certified regulatory measurement (see module
    docstring) — use it for regression ("did this preset change widen the
    occupied bandwidth"), not for compliance sign-off.
    """
    import numpy as np  # lazy: ships with the [sdr] extra

    freqs, psd_db = power_spectrum_db(iq, sample_rate_hz, nfft=nfft)
    peak_idx = int(np.argmax(psd_db))
    peak_db = psd_db[peak_idx]
    threshold = peak_db - db_down

    low_idx = peak_idx
    while low_idx > 0 and psd_db[low_idx - 1] >= threshold:
        low_idx -= 1
    high_idx = peak_idx
    while high_idx < len(psd_db) - 1 and psd_db[high_idx + 1] >= threshold:
        high_idx += 1

    return OccupiedBandwidth(
        low_hz=float(freqs[low_idx]),
        high_hz=float(freqs[high_idx]),
        bw_hz=float(freqs[high_idx] - freqs[low_idx]),
        peak_freq_hz=float(freqs[peak_idx]),
    )


def in_band_fraction(
    iq: np.ndarray,
    sample_rate_hz: float,
    *,
    capture_center_hz: float,
    band_start_hz: float,
    band_end_hz: float,
    nfft: int = 4096,
) -> float:
    """Fraction (0..1) of total spectral power that falls within an absolute
    frequency band (e.g. a region's `[freq_start_mhz, freq_end_mhz]`).

    Use this for the "did the device emit anything outside its allocated
    region" check: capture wide enough to see both the expected channel and
    some guard band beyond the region's edges, then confirm this fraction is
    ~1.0. A low fraction with a confirmed-active capture means energy leaked
    outside the configured region.
    """
    import numpy as np  # lazy: ships with the [sdr] extra

    freqs, psd_db = power_spectrum_db(iq, sample_rate_hz, nfft=nfft)
    psd_linear = 10.0 ** (psd_db / 10.0)
    abs_freqs = capture_center_hz + freqs
    in_band = (abs_freqs >= band_start_hz) & (abs_freqs <= band_end_hz)
    total = float(np.sum(psd_linear))
    if total <= 0:
        return 0.0
    return float(np.sum(psd_linear[in_band]) / total)


@dataclass(frozen=True)
class ActiveWindow:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def active_windows(
    iq: np.ndarray,
    sample_rate_hz: float,
    *,
    threshold_db: float = 10.0,
    win_samples: int = 256,
    min_gap_s: float = 0.002,
) -> list[ActiveWindow]:
    """Detect RF-active time segments via a sliding-window power envelope.

    Computes mean power per `win_samples`-sample window, estimates the noise
    floor as the median window power, and flags windows more than
    `threshold_db` above it as active. Adjacent active windows separated by a
    gap shorter than `min_gap_s` are merged (avoids splitting one transmission
    into many segments across a brief envelope dip). This is the input to
    `duty_cycle_pct` — it does not require demodulating the signal, only
    detecting "is the radio transmitting right now."
    """
    import numpy as np  # lazy: ships with the [sdr] extra

    n = len(iq)
    n_windows = n // win_samples
    if n_windows == 0:
        return []
    power = np.abs(iq[: n_windows * win_samples].reshape(n_windows, win_samples)) ** 2
    win_power_db = 10.0 * np.log10(np.mean(power, axis=1) + 1e-20)
    noise_floor_db = float(np.median(win_power_db))
    active_mask = win_power_db >= (noise_floor_db + threshold_db)

    win_duration_s = win_samples / sample_rate_hz
    windows: list[ActiveWindow] = []
    start_idx: int | None = None
    for i, is_active in enumerate(active_mask):
        if is_active and start_idx is None:
            start_idx = i
        elif not is_active and start_idx is not None:
            windows.append(ActiveWindow(start_idx * win_duration_s, i * win_duration_s))
            start_idx = None
    if start_idx is not None:
        windows.append(ActiveWindow(start_idx * win_duration_s, n_windows * win_duration_s))

    # Merge windows separated by a gap shorter than min_gap_s.
    merged: list[ActiveWindow] = []
    for w in windows:
        if merged and (w.start_s - merged[-1].end_s) < min_gap_s:
            merged[-1] = ActiveWindow(merged[-1].start_s, w.end_s)
        else:
            merged.append(w)
    return merged


def duty_cycle_pct(windows: Sequence[ActiveWindow], observation_window_s: float) -> float:
    """Percent of `observation_window_s` spent RF-active, from `active_windows()` output."""
    if observation_window_s <= 0:
        return 0.0
    active_s = sum(w.duration_s for w in windows)
    return 100.0 * active_s / observation_window_s
