# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Closed-loop PA calibration: step a Meshtastic node's configured `lora.tx_power`
and measure the actual power off its PA with an ImmersionRC meter (`power_meter.py`),
producing a configured-vs-measured table and a compression/saturation analysis.

This answers the question neither the SDR oracle nor the firmware can: *does the
radio actually emit the power it's told to, and where does the PA stop tracking?*
`rf_oracle.confirm_tx` tells you whether RF left the antenna at the right
frequency; this tells you **how much** power, in absolute dBm, at each configured
step — the PA gain curve and its saturation point.

Flow per step (`sweep`):
  1. set `lora.tx_power` on the node (optionally reboot for pre-2.8.0 firmware
     that doesn't apply LoRa config live),
  2. key a multi-second TX burst (queued ~200 B broadcasts — an idle node rarely
     transmits, and EU868 duty-cycle limits are overridden for the bench run),
  3. sample the meter's average power throughout the burst and keep only samples
     that clear the pre-captured noise floor (TX-active discrimination),
  4. correct for the external attenuator (in software — the meter never sees it)
     and record configured vs measured.

The pure functions (`classify_active`, `summarize_step`, `analyze_curve`) contain
all the measurement logic and are unit-tested without hardware
(`tests/unit/test_power_meter.py`). Everything is a bench regression check with a
~±0.5 dB instrument and a hand-entered attenuator value — not a certified
measurement (see `power_meter.py`).
"""

from __future__ import annotations

import itertools
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import admin, power_meter
from .rf_oracle import read_lora_context


class PaSweepError(RuntimeError):
    pass


# A ~200 B text broadcast is near the Meshtastic payload ceiling and gives the
# longest single-packet airtime; repeating it sustains the meter's view of the PA.
_BURST_TEXT = "PA-SWEEP " + "x" * 190


# ---------------------------------------------------------------------------
# Pure measurement logic — no hardware, fully unit-testable.
# ---------------------------------------------------------------------------
def classify_active(
    samples: list[float], floor_dbm: float, *, margin_db: float = 5.0
) -> list[float]:
    """Keep only samples that clear the noise floor by `margin_db` — i.e. the
    ones taken while the PA was actually transmitting.

    The meter's log-detector floor sits around -25 dBm even with no input, so a
    plain max/mean over a capture window blurs TX bursts together with idle
    time. Thresholding at ``floor + margin`` isolates the TX-active samples the
    same way `sdr.active_windows` isolates active time segments.
    """
    threshold = floor_dbm + margin_db
    return [s for s in samples if s >= threshold]


@dataclass(frozen=True)
class StepResult:
    configured_dbm: int
    measured_avg_dbm: float | None  # None when no TX-active sample was captured
    measured_peak_dbm: float | None
    active_samples: int
    total_samples: int

    @property
    def rf_observed(self) -> bool:
        return self.measured_avg_dbm is not None


def summarize_step(
    samples: list[float],
    configured_dbm: int,
    floor_dbm: float,
    *,
    attenuator_db: float = 0.0,
    margin_db: float = 5.0,
) -> StepResult:
    """Reduce one step's raw meter samples to a `StepResult`, applying the
    attenuator correction (added back in software — the pad sits between the PA
    and the meter, so true power = meter reading + attenuator_db).
    """
    active = classify_active(samples, floor_dbm, margin_db=margin_db)
    if not active:
        return StepResult(configured_dbm, None, None, 0, len(samples))
    avg = statistics.fmean(active) + attenuator_db
    peak = max(active) + attenuator_db
    return StepResult(configured_dbm, avg, peak, len(active), len(samples))


def _step_row(s: StepResult) -> dict[str, Any]:
    """One `sweep` table row — measured fields are None when the step was silent."""
    avg, peak = s.measured_avg_dbm, s.measured_peak_dbm
    observed = avg is not None and peak is not None
    return {
        "configured_dbm": s.configured_dbm,
        "measured_avg_dbm": round(avg, 2) if avg is not None else None,
        "measured_peak_dbm": round(peak, 2) if peak is not None else None,
        "delta_db": round(avg - s.configured_dbm, 2) if avg is not None else None,
        "active_samples": s.active_samples,
        "total_samples": s.total_samples,
        "rf_observed": observed,
    }


def analyze_curve(steps: list[StepResult], *, compression_ratio: float = 0.5) -> dict[str, Any]:
    """Characterize the PA gain curve from the per-step measurements.

    Reports where the PA stops tracking the configured setting: the
    **saturation point** is the first configured step whose incremental measured
    gain per +1 dB configured falls below `compression_ratio` (a linear PA tracks
    ~1.0; a saturated one flattens toward 0). Also reports the offset at the
    lowest step (measured − configured — the PA/system gain error after
    attenuator correction), peak measured power, and whether the curve is
    monotonic.
    """
    usable = [s for s in steps if s.measured_avg_dbm is not None]
    if len(usable) < 2:
        first = usable[0].measured_avg_dbm if usable else None
        return {
            "points": len(usable),
            "saturation_dbm": None,
            "max_measured_dbm": first,
            "offset_at_min_db": (first - usable[0].configured_dbm if first is not None else None),
            "monotonic": True,
            "note": "need >=2 measured steps for a curve",
        }

    usable.sort(key=lambda s: s.configured_dbm)
    saturation_dbm: int | None = None
    monotonic = True
    for prev, cur in itertools.pairwise(usable):
        d_cfg = cur.configured_dbm - prev.configured_dbm
        assert cur.measured_avg_dbm is not None and prev.measured_avg_dbm is not None
        d_meas = cur.measured_avg_dbm - prev.measured_avg_dbm
        if d_meas < -0.5:
            monotonic = False
        if saturation_dbm is None and d_cfg > 0 and (d_meas / d_cfg) < compression_ratio:
            saturation_dbm = prev.configured_dbm

    lowest = usable[0]
    highest = max(usable, key=lambda s: s.measured_avg_dbm)  # type: ignore[arg-type,return-value]
    assert lowest.measured_avg_dbm is not None and highest.measured_avg_dbm is not None
    return {
        "points": len(usable),
        "saturation_dbm": saturation_dbm,
        "max_measured_dbm": round(highest.measured_avg_dbm, 2),
        "max_measured_at_configured_dbm": highest.configured_dbm,
        "offset_at_min_db": round(lowest.measured_avg_dbm - lowest.configured_dbm, 2),
        "monotonic": monotonic,
    }


# ---------------------------------------------------------------------------
# Hardware orchestration.
# ---------------------------------------------------------------------------
@dataclass
class _BackgroundSampler:
    """Continuously reads the meter's average power in a thread until stopped.

    Only this thread touches the meter's serial port during a burst; the main
    thread drives the Meshtastic node (a *different* port), so there's no
    contention — but never sample the same meter from two threads at once.
    """

    meter: power_meter.PowerMeter
    interval_s: float = 0.05
    _samples: list[float] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _error: str | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._samples.append(self.meter.read_avg_dbm())
            except power_meter.PowerMeterError as exc:
                self._error = str(exc)
                return
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> list[float]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._error is not None:
            raise PaSweepError(f"meter sampling failed mid-burst: {self._error}")
        return list(self._samples)


def measure(
    band: str,
    *,
    samples: int = 20,
    interval_s: float = 0.05,
    attenuator_db: float = 0.0,
    meter_port: str | None = None,
    peak: bool = False,
    persist_freq: bool = False,
) -> dict[str, Any]:
    """One-shot passive read of whatever the meter currently sees at `band`.

    No Meshtastic device involved — activates the band's calibration curve, takes
    `samples` readings, and returns min/mean/max (attenuator-corrected). Use it to
    read the noise floor, sanity-check a signal generator, or spot-check a TX that
    something else is keying.
    """
    freq_mhz = power_meter.band_to_freq_mhz(band)
    with power_meter.PowerMeter(meter_port) as m:
        m.version()  # fail fast if it's not actually an ImmersionRC meter
        m.set_freq_mhz(freq_mhz, persist=persist_freq)
        readings = m.sample(samples, interval_s=interval_s, peak=peak)
    corrected = [r + attenuator_db for r in readings]
    return {
        "band": band,
        "freq_mhz": freq_mhz,
        "kind": "peak" if peak else "average",
        "attenuator_db": attenuator_db,
        "samples": len(corrected),
        "min_dbm": round(min(corrected), 2),
        "mean_dbm": round(statistics.fmean(corrected), 2),
        "max_dbm": round(max(corrected), 2),
    }


def status(meter_port: str | None = None) -> dict[str, Any]:
    """Detect the meter and report version, stored frequency, and a live reading.

    Read-only probe — the bench equivalent of `recorder_status`. Returns
    ``{"present": False}`` (never raises) when no meter is attached.
    """
    port = meter_port or power_meter.find_meter_port()
    if port is None:
        return {"present": False, "detail": "no ImmersionRC meter (VID 0x04D8/PID 0x000A) attached"}
    try:
        with power_meter.PowerMeter(port) as m:
            info = m.info()
            avg = m.read_avg_dbm()
            peak = m.read_peak_dbm()
    except power_meter.PowerMeterError as exc:
        return {"present": True, "port": port, "error": str(exc)}
    return {
        "present": True,
        "port": info.port,
        "version": info.version,
        "stored_freq_mhz": info.stored_freq_mhz,
        "current_avg_dbm": round(avg, 2),
        "current_peak_dbm": round(peak, 2),
    }


def _key_burst(text: str, channel_index: int, port: str | None, repeat: int, gap_s: float) -> int:
    """Queue `repeat` broadcast bursts to sustain TX airtime; return packets queued."""
    queued = 0
    for i in range(repeat):
        admin.send_text(text=text, channel_index=channel_index, port=port)
        queued += 1
        if i + 1 < repeat:
            time.sleep(gap_s)
    return queued


def sweep(
    powers: list[int],
    band: str | None = None,
    *,
    port: str | None = None,
    meter_port: str | None = None,
    channel_index: int = 0,
    attenuator_db: float = 0.0,
    burst_repeat: int = 3,
    burst_gap_s: float = 0.3,
    settle_s: float = 1.5,
    floor_samples: int = 20,
    floor_margin_db: float = 5.0,
    sample_interval_s: float = 0.05,
    reboot_between_steps: bool = False,
    reboot_wait_s: float = 20.0,
    override_duty_cycle: bool = True,
    restore_config: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Step `lora.tx_power` through `powers` and measure the PA output at each.

    Destructive/closed-loop: mutates the node's LoRa config, keys real TX onto
    the mesh, and (optionally) reboots the node between steps. Requires
    ``confirm=True``.

    `band` defaults to the node's configured region (read live). `attenuator_db`
    is the pad between the PA and the meter (added back in software). The result
    is a table of configured vs measured dBm plus `analyze_curve`'s
    saturation/gain summary.

    Safety: refuses any step whose *expected* meter input (configured power minus
    attenuator) would exceed the meter's absolute max — protect the instrument
    with an adequate pad. Restores the original `tx_power` and duty-cycle override
    on exit unless ``restore_config=False``.
    """
    if not confirm:
        raise PaSweepError(
            "pa_sweep mutates lora.tx_power, keys TX onto the mesh, and may reboot the "
            "node — pass confirm=True to proceed."
        )
    if not powers:
        raise PaSweepError("powers list is empty")

    ctx = read_lora_context(port=port)
    region = ctx.get("region", "UNSET")
    if band is None:
        band = region
    freq_mhz = power_meter.band_to_freq_mhz(band)

    # Protect the meter: the pad must knock the highest configured step below the
    # meter's absolute max. (Approximate — ignores PA gain error, which is what
    # we're measuring — so this is a floor, not a guarantee. Use a generous pad.)
    max_expected_input = max(powers) - attenuator_db
    if max_expected_input > power_meter.METER_ABS_MAX_DBM:
        raise PaSweepError(
            f"max configured {max(powers)} dBm minus {attenuator_db} dB pad = "
            f"{max_expected_input:.1f} dBm exceeds the meter's {power_meter.METER_ABS_MAX_DBM} dBm "
            "absolute max. Add more attenuation before sweeping."
        )

    original_tx_power = ctx.get("tx_power", 0)
    duty_override_touched = False
    steps: list[StepResult] = []

    with power_meter.PowerMeter(meter_port) as m:
        m.version()
        m.set_freq_mhz(freq_mhz)

        # Floor: no TX in flight, so read the meter directly (no sampler thread).
        floor_readings = m.sample(floor_samples, interval_s=sample_interval_s)
        floor_dbm = statistics.median(floor_readings)

        if override_duty_cycle:
            admin.set_config("lora.override_duty_cycle", True, port=port)
            duty_override_touched = True

        try:
            for p in powers:
                admin.set_config("lora.tx_power", int(p), port=port)
                if reboot_between_steps:
                    admin.reboot(port=port, confirm=True)
                    time.sleep(reboot_wait_s)
                else:
                    time.sleep(settle_s)

                sampler = _BackgroundSampler(m, interval_s=sample_interval_s)
                sampler.start()
                _key_burst(_BURST_TEXT, channel_index, port, burst_repeat, burst_gap_s)
                # Let the queued bursts drain onto the air before we stop watching.
                time.sleep(settle_s)
                raw = sampler.stop()

                steps.append(
                    summarize_step(
                        raw,
                        int(p),
                        floor_dbm,
                        attenuator_db=attenuator_db,
                        margin_db=floor_margin_db,
                    )
                )
        finally:
            if restore_config:
                admin.set_config("lora.tx_power", int(original_tx_power), port=port)
                if duty_override_touched:
                    admin.set_config("lora.override_duty_cycle", False, port=port)

    table = [_step_row(s) for s in steps]
    silent_steps = [s.configured_dbm for s in steps if not s.rf_observed]

    return {
        "band": band,
        "region": region,
        "freq_mhz": freq_mhz,
        "attenuator_db": attenuator_db,
        "floor_dbm": round(floor_dbm, 2),
        "floor_margin_db": floor_margin_db,
        "table": table,
        "curve": analyze_curve(steps),
        "silent_steps_dbm": silent_steps,
        "config_restored": restore_config,
        "caveat": (
            "Bench regression check: ImmersionRC meter (~±0.5 dB) with a hand-entered "
            "attenuator value; not a calibrated/certified measurement. Steps with no TX-active "
            "sample (silent_steps_dbm) may be a dead PA, but under airtime pressure a queued "
            "packet can also transmit after the sampling window closes — re-run spaced out "
            "before concluding a step is truly silent."
        ),
    }
