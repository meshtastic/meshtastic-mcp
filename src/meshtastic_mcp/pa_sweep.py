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
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import admin, lora_compliance, power_meter
from .rf_oracle import read_lora_context


class PaSweepError(RuntimeError):
    pass


# A ~200 B text broadcast is near the Meshtastic payload ceiling and gives the
# longest single-packet airtime; repeating it sustains the meter's view of the PA.
_BURST_TEXT = "PA-SWEEP " + "x" * 190

# Bytes the firmware adds around the text before it goes on air: the MeshPacket
# header (~16 B) plus the Data submessage / encryption framing. Slightly generous
# on purpose — over-estimating airtime lengthens the linger, which is the safe
# direction (a short linger clips the TX; a long one only costs time).
_MESH_PACKET_OVERHEAD_BYTES = 32

# Firmware's channel-politeness delay before it keys a broadcast (send_text.py
# documents ~4 s), plus a margin. The linger must cover this delay *plus* the
# packet's airtime, since send_text returns as soon as the packet is queued.
_TX_POLITENESS_S = 4.0
_TX_LINGER_MARGIN_S = 1.0
# Used only if the live preset can't be predicted (unknown region / malformed
# ctx) — sizing the linger must never crash a sweep.
_FALLBACK_TX_LINGER_S = 8.0


def lora_time_on_air_s(
    payload_bytes: int,
    sf: int,
    bw_khz: float,
    cr: int,
    *,
    preamble_symbols: int = 16,
    explicit_header: bool = True,
    crc: bool = True,
) -> float:
    """LoRa time-on-air (seconds) via the Semtech AN1200.13 formula.

    `cr` is the Meshtastic coding-rate denominator (5..8 for 4/5..4/8).
    `preamble_symbols` defaults to 16 (Meshtastic's setting). Low-data-rate
    optimization is applied automatically when the symbol time exceeds 16 ms
    (SF11/125 kHz and SF12), matching firmware. Used to size the sweep's TX
    linger so a burst isn't cut off before it finishes transmitting.
    """
    bw_hz = bw_khz * 1000.0
    t_sym = (2**sf) / bw_hz
    low_data_rate_opt = 1 if t_sym > 16e-3 else 0
    header = 0 if explicit_header else 1
    crc_on = 1 if crc else 0
    cr_val = cr - 4  # 5..8 (4/5..4/8) -> 1..4
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16 * crc_on - 20 * header
    denominator = 4 * (sf - 2 * low_data_rate_opt)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cr_val + 4), 0)
    t_preamble = (preamble_symbols + 4.25) * t_sym
    t_payload = payload_symbols * t_sym
    return t_preamble + t_payload


def _derive_tx_linger_s(ctx: dict[str, Any]) -> float:
    """Size the per-burst TX linger from the node's live LoRa preset.

    Predicts SF/BW/CR from `ctx` (same path as `rf_oracle`), computes the burst's
    time-on-air, and returns politeness delay + airtime + margin — so a fast
    preset gets a short linger and a slow one (LONG_SLOW: multi-second airtime)
    gets a long enough linger not to clip. Falls back to a safe constant if the
    preset can't be predicted; linger sizing must never crash a sweep.
    """
    try:
        pred = lora_compliance.predict_lora_params(
            ctx["region"],
            ctx.get("modem_preset", "LONG_FAST"),
            channel_name=ctx.get("channel_name", ""),
            channel_num=ctx.get("channel_num", 0),
            use_preset=ctx.get("use_preset", True),
            bandwidth_khz=ctx.get("bandwidth"),
            spread_factor=ctx.get("spread_factor"),
            coding_rate=ctx.get("coding_rate"),
            override_frequency_mhz=ctx.get("override_frequency", 0.0),
            frequency_offset_mhz=ctx.get("frequency_offset", 0.0),
            device_role=ctx.get("device_role", "CLIENT"),
        )
        toa = lora_time_on_air_s(
            len(_BURST_TEXT) + _MESH_PACKET_OVERHEAD_BYTES, pred.sf, pred.bw_khz, pred.cr
        )
        return _TX_POLITENESS_S + toa + _TX_LINGER_MARGIN_S
    except Exception:
        return _FALLBACK_TX_LINGER_S


def resolve_band_mhz(band: str) -> float:
    """Resolve a band to a frequency in MHz for the meter.

    Accepts either a Meshtastic **region enum name** (``"US"``, ``"EU_868"``,
    ``"EU_433"``, ``"JP"`` ...) — resolved to the centre of that region's
    allocation via `lora_compliance.REGIONS`, the same table firmware derives its
    channels from — or a bare MHz value (``"868"``, ``"915"``). Region names are
    what `rf_oracle.read_lora_context` reports, so `sweep(band=None)` feeds one
    straight through here. The meter has no continuous tuning, so whatever this
    returns is snapped to the nearest stored calibration point by the driver.
    """
    key = band.strip().upper()
    info = lora_compliance.REGIONS.get(key)
    if info is not None:
        return (info.freq_start_mhz + info.freq_end_mhz) / 2.0
    try:
        return float(band)
    except ValueError as exc:
        raise PaSweepError(
            f"Unknown band {band!r}: pass a MHz value or a Meshtastic region name "
            f"(e.g. US, EU_868, EU_433, JP, ANZ)."
        ) from exc


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


def _avg(s: StepResult) -> float:
    """The measured average, asserted non-None (callers filter to measured steps)."""
    v = s.measured_avg_dbm
    assert v is not None
    return v


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
        d_meas = _avg(cur) - _avg(prev)
        if d_meas < -0.5:
            monotonic = False
        if saturation_dbm is None and d_cfg > 0 and (d_meas / d_cfg) < compression_ratio:
            saturation_dbm = prev.configured_dbm

    lowest = usable[0]
    highest = max(usable, key=_avg)
    return {
        "points": len(usable),
        "saturation_dbm": saturation_dbm,
        "max_measured_dbm": round(_avg(highest), 2),
        "max_measured_at_configured_dbm": highest.configured_dbm,
        "offset_at_min_db": round(_avg(lowest) - lowest.configured_dbm, 2),
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
    `samples` readings, and returns min/mean/max in dBm. `band` is a Meshtastic
    region name (``"US"``, ``"EU_868"`` ...) or a bare MHz value (see
    `resolve_band_mhz`). Use it to read the noise floor, sanity-check a signal
    generator, or spot-check a TX that something else is keying.

    `attenuator_db` is added to every reading (the pad sits between the source and
    the meter). That's what you want for a signal, but note it also inflates a
    noise-floor read by the pad value — pass ``attenuator_db=0`` if you want the
    meter's raw floor rather than the floor referred to the pad's input.
    """
    center_mhz = resolve_band_mhz(band)
    with power_meter.PowerMeter(meter_port) as m:
        m.version()  # fail fast if it's not actually an ImmersionRC meter
        cal_mhz = m.set_freq_mhz(center_mhz, persist=persist_freq)
        readings = m.sample(samples, interval_s=interval_s, peak=peak)
    corrected = [r + attenuator_db for r in readings]
    return {
        "band": band,
        "requested_center_mhz": round(center_mhz, 3),
        "meter_cal_mhz": cal_mhz,
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


def _key_burst(
    text: str,
    channel_index: int,
    port: str | None,
    repeat: int,
    gap_s: float,
    tx_linger_s: float,
) -> int:
    """Queue `repeat` broadcast bursts to sustain TX airtime; return packets queued.

    Each `send_text` holds the node's port open for `tx_linger_s` after the send
    so the firmware's channel-politeness delay (~4 s for broadcasts) and the
    packet's airtime finish before the close-triggered DTR reset drops the queued
    TX. We pass this explicitly rather than inheriting `send_text`'s
    send-a-message default (8 s): here it's paid `repeat` times per step and the
    meter sampler is watching throughout, so the sweep tunes it to just cover the
    politeness delay plus one packet's airtime instead of the conservative
    interactive default. Slow presets (long airtime) need a higher value — see
    `sweep`'s `tx_linger_s`.
    """
    queued = 0
    for i in range(repeat):
        admin.send_text(text=text, channel_index=channel_index, port=port, tx_linger_s=tx_linger_s)
        queued += 1
        if i + 1 < repeat:
            time.sleep(gap_s)
    return queued


def _restore_config(
    path: str,
    value: object,
    port: str | None,
    errors: list[str],
    *,
    attempts: int = 3,
    backoff_s: float = 0.5,
) -> None:
    """Best-effort restore of one config field, retrying the busy case.

    Each restore opens the node's serial port, and per the "one MCP call per
    serial port" rule that acquisition is non-blocking and fails fast with a
    *busy* error. Restoring `override_duty_cycle` in particular has regulatory
    consequences if it's left on, so we retry the busy case a few times and, on
    final failure, record it in `errors` instead of raising — so one field's
    failure never skips the next field's restore, and the caller can surface what
    didn't come back rather than leaving it silently wrong.
    """
    for i in range(attempts):
        try:
            admin.set_config(path, value, port=port)
            return
        except Exception as exc:
            if "busy" in str(exc).lower() and i + 1 < attempts:
                time.sleep(backoff_s)
                continue
            errors.append(f"{path}: {exc}")
            return


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
    tx_linger_s: float | None = None,
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

    `band` defaults to the node's configured region (read live) — a Meshtastic
    region name like ``"US"`` or ``"EU_868"``, resolved to a frequency via
    `resolve_band_mhz`. `attenuator_db` is the pad between the PA and the meter
    (added back in software). The result is a table of configured vs measured dBm
    plus `analyze_curve`'s saturation/gain summary.

    Each step keys `burst_repeat` broadcasts, holding the node's port open
    `tx_linger_s` per send so the firmware's ~4 s broadcast politeness delay plus
    the packet's airtime finish before close (without it, queued TX is lost).
    `tx_linger_s=None` (default) **auto-derives** it from the live preset's
    time-on-air (`_derive_tx_linger_s`): politeness + airtime + margin, so a fast
    preset gets a short linger and LONG_SLOW (multi-second airtime) gets a long
    enough one — no clipping, no manual tuning. Pass a number to override. This is
    paid once per burst per step and dominates sweep wall-clock; the value used is
    reported as `tx_linger_s` in the result.

    Safety: refuses any step whose *expected* meter input (configured power minus
    attenuator) would exceed the meter's absolute max — protect the instrument
    with an adequate pad. Restores the original `tx_power` and duty-cycle override
    on exit unless ``restore_config=False``; each restore is independent and its
    failure is surfaced in ``restore_errors`` rather than silently swallowed.
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
    center_mhz = resolve_band_mhz(band)
    linger_s = tx_linger_s if tx_linger_s is not None else _derive_tx_linger_s(ctx)

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

    # 0 is firmware's "use the region's default power" sentinel, not literally
    # 0 dBm — restoring it hands the node back to its default, which is correct.
    original_tx_power = ctx.get("tx_power", 0)
    original_duty_override = bool(ctx.get("override_duty_cycle", False))
    duty_override_touched = False
    restore_errors: list[str] = []
    steps: list[StepResult] = []

    with power_meter.PowerMeter(meter_port) as m:
        m.version()
        cal_mhz = m.set_freq_mhz(center_mhz)

        # Floor: no TX in flight, so read the meter directly (no sampler thread).
        floor_readings = m.sample(floor_samples, interval_s=sample_interval_s)
        floor_dbm = statistics.median(floor_readings)

        # Only flip the duty-cycle override if it isn't already where we need it —
        # then we know the original value to put back, and we never write a device
        # that was already configured the way we want.
        if override_duty_cycle and not original_duty_override:
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
                _key_burst(_BURST_TEXT, channel_index, port, burst_repeat, burst_gap_s, linger_s)
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
            # Restore each field independently: a busy/transient failure on one
            # must not skip the next (leaving override_duty_cycle stuck on is the
            # regulatory hazard). Failures land in restore_errors, reported below.
            if restore_config:
                _restore_config("lora.tx_power", int(original_tx_power), port, restore_errors)
                if duty_override_touched:
                    _restore_config(
                        "lora.override_duty_cycle", original_duty_override, port, restore_errors
                    )

    table = [_step_row(s) for s in steps]
    silent_steps = [s.configured_dbm for s in steps if not s.rf_observed]

    return {
        "band": band,
        "region": region,
        "requested_center_mhz": round(center_mhz, 3),
        "meter_cal_mhz": cal_mhz,
        "attenuator_db": attenuator_db,
        "tx_linger_s": round(linger_s, 2),
        "floor_dbm": round(floor_dbm, 2),
        "floor_margin_db": floor_margin_db,
        "table": table,
        "curve": analyze_curve(steps),
        "silent_steps_dbm": silent_steps,
        "config_restored": restore_config,
        "restore_errors": restore_errors,
        "caveat": (
            "Bench regression check: ImmersionRC meter (~±0.5 dB) with a hand-entered "
            "attenuator value; not a calibrated/certified measurement. Steps with no TX-active "
            "sample (silent_steps_dbm) may be a dead PA, but under airtime pressure a queued "
            "packet can also transmit after the sampling window closes — re-run spaced out "
            "before concluding a step is truly silent."
        ),
    }
