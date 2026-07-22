# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ImmersionRC power-meter driver protocol + PA-sweep analysis, no hardware.

The wire protocol (`PowerMeter._cmd`) is exercised against a fake serial port
that mimics the meter's ``<value>\\r\\n`` + ``OK\\r\\n`` reply grammar (verified
live against fw 1.0.11 — see `IMMERSIONRC_METER_HANDOFF.md`). The measurement
logic (`classify_active`, `summarize_step`, `analyze_curve`) is pure and tested
on synthetic sample sets, the same way `test_sdr.py` tests the SDR analysis on
synthetic IQ — this is the regression net for the bench-calibration path.
"""

from __future__ import annotations

import pytest

from meshtastic_mcp import pa_sweep, power_meter


# ---------------------------------------------------------------------------
# Fake serial port: canned replies keyed by the command written to it.
# ---------------------------------------------------------------------------
class FakeSerial:
    """Minimal stand-in for `serial.Serial` speaking the meter's line protocol."""

    def __init__(self, replies: dict[str, list[str]]) -> None:
        # replies maps a command (without newline) -> the lines it answers with.
        self._replies = replies
        self._outbox: list[str] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        cmd = data.decode("ascii").strip()
        lines = self._replies.get(cmd)
        if lines is None:
            lines = ["ERROR"]
        self._outbox.extend(lines)
        return len(data)

    def readline(self) -> bytes:
        if not self._outbox:
            return b""  # timeout / no data
        return (self._outbox.pop(0) + "\r\n").encode("ascii")

    def close(self) -> None:
        self.closed = True


def _meter_with(replies: dict[str, list[str]]) -> power_meter.PowerMeter:
    m = power_meter.PowerMeter.__new__(power_meter.PowerMeter)
    m._port = "COM-TEST"  # type: ignore[attr-defined]
    m._baud = 9600  # type: ignore[attr-defined]
    m._timeout = 0.5  # type: ignore[attr-defined]
    m._ser = FakeSerial(replies)  # type: ignore[attr-defined]
    return m


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
def test_value_reply_consumes_trailing_ok() -> None:
    m = _meter_with({"D": ["-26.450439", "OK"]})
    assert m.read_avg_dbm() == pytest.approx(-26.450439)


def test_peak_reply() -> None:
    m = _meter_with({"E": ["-25.9596", "OK"]})
    assert m.read_peak_dbm() == pytest.approx(-25.9596)


def test_version_validated() -> None:
    m = _meter_with({"V": ["RFPowerMeterv2 1.0.11", "OK"]})
    assert "1.0.11" in m.version()


def test_version_rejects_foreign_device() -> None:
    m = _meter_with({"V": ["SomeOtherDevice", "OK"]})
    with pytest.raises(power_meter.PowerMeterError):
        m.version()


def test_persist_returns_ok_only() -> None:
    # `S` answers with a bare OK (no value line); _cmd must not block waiting for a value.
    m = _meter_with({"S": ["OK"]})
    assert m._cmd("S") == "OK"


def test_unknown_command_raises() -> None:
    m = _meter_with({})  # every command -> ERROR
    with pytest.raises(power_meter.PowerMeterError, match="ERROR"):
        m.read_avg_dbm()


def test_empty_reply_flags_powered_off() -> None:
    # An auto-off meter answers nothing (readline times out -> "").
    m = _meter_with({"D": []})
    with pytest.raises(power_meter.PowerMeterError, match=r"powered off|no reply"):
        m.read_avg_dbm()


def test_set_freq_index_echoes_and_consumes_cleanly(monkeypatch) -> None:
    monkeypatch.setattr(power_meter.time, "sleep", lambda _s: None)  # skip the 200 ms gap
    m = _meter_with({"F3": ["868", "OK"], "S": ["OK"]})
    ser: FakeSerial = m._ser  # type: ignore[assignment]
    echoed = m.set_freq_index(3, persist=True)
    assert echoed == 868
    assert ser._outbox == []  # every value+OK pair (F3 twice) and the S/OK fully consumed


def test_set_freq_index_out_of_range() -> None:
    m = _meter_with({})
    with pytest.raises(power_meter.PowerMeterError, match="out of range"):
        m.set_freq_index(99)


# ---------------------------------------------------------------------------
# Frequency mapping
# ---------------------------------------------------------------------------
def test_nearest_freq_index_snaps() -> None:
    assert power_meter.FREQ_INDEX_MHZ[power_meter.nearest_freq_index(868)] == 868
    assert power_meter.FREQ_INDEX_MHZ[power_meter.nearest_freq_index(915)] == 900  # closest point
    assert power_meter.FREQ_INDEX_MHZ[power_meter.nearest_freq_index(5805)] == 5800


def test_band_to_freq_mhz() -> None:
    assert power_meter.band_to_freq_mhz("EU868") == 868
    assert power_meter.band_to_freq_mhz("US915") == 900
    assert power_meter.band_to_freq_mhz("868") == 868
    with pytest.raises(power_meter.PowerMeterError):
        power_meter.band_to_freq_mhz("NOTABAND")


def test_list_meters_never_raises(monkeypatch) -> None:
    def boom() -> list:
        raise RuntimeError("usb wedged")

    monkeypatch.setattr(power_meter.list_ports, "comports", boom)
    assert power_meter.list_meters() == []


# ---------------------------------------------------------------------------
# Pure measurement logic
# ---------------------------------------------------------------------------
def test_classify_active_filters_floor() -> None:
    floor = -25.0
    samples = [-25.1, -24.8, -10.0, -9.5, -24.9]  # two real TX samples, rest floor
    active = pa_sweep.classify_active(samples, floor, margin_db=5.0)
    assert active == [-10.0, -9.5]


def test_summarize_step_applies_attenuator() -> None:
    # Meter reads ~ -10 dBm through a 30 dB pad => true power ~ +20 dBm.
    samples = [-25.0, -10.0, -9.0, -24.0]
    step = pa_sweep.summarize_step(samples, 20, -25.0, attenuator_db=30.0, margin_db=5.0)
    assert step.rf_observed
    assert step.measured_avg_dbm == pytest.approx(30.0 + (-9.5))  # mean of active + pad
    assert step.measured_peak_dbm == pytest.approx(30.0 + (-9.0))
    assert step.active_samples == 2
    assert step.total_samples == 4


def test_summarize_step_no_active_sample_is_silent() -> None:
    step = pa_sweep.summarize_step([-25.0, -24.9], 5, -25.0)
    assert not step.rf_observed
    assert step.measured_avg_dbm is None


def _step(cfg: int, meas: float | None) -> pa_sweep.StepResult:
    return pa_sweep.StepResult(cfg, meas, meas, 5 if meas is not None else 0, 5)


def test_analyze_curve_finds_saturation() -> None:
    # Linear up to 17 dBm, then the PA compresses (measured barely moves).
    steps = [
        _step(5, 5.0),
        _step(11, 11.0),
        _step(17, 17.0),
        _step(20, 17.4),  # +3 configured -> +0.4 measured => ratio 0.13 < 0.5
        _step(22, 17.5),
    ]
    curve = pa_sweep.analyze_curve(steps)
    assert curve["saturation_dbm"] == 17
    assert curve["max_measured_dbm"] == pytest.approx(17.5)
    assert curve["monotonic"] is True


def test_analyze_curve_offset_at_min() -> None:
    steps = [_step(5, 3.0), _step(20, 18.0)]  # 2 dB system loss at the bottom
    curve = pa_sweep.analyze_curve(steps)
    assert curve["offset_at_min_db"] == pytest.approx(-2.0)


def test_analyze_curve_needs_two_points() -> None:
    curve = pa_sweep.analyze_curve([_step(20, 20.0), _step(22, None)])
    assert curve["saturation_dbm"] is None
    assert "need >=2" in curve["note"]


def test_sweep_requires_confirm() -> None:
    with pytest.raises(pa_sweep.PaSweepError, match="confirm=True"):
        pa_sweep.sweep([20], band="EU868", confirm=False)
