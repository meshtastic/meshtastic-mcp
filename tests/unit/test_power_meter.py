# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ImmersionRC power-meter driver protocol + PA-sweep analysis, no hardware.

The wire protocol (`PowerMeter._cmd`) is exercised against a fake serial port
that mimics the meter's ``<value>\\r\\n`` + ``OK\\r\\n`` reply grammar (verified
live against fw 1.0.11 — see `docs/power-meter.md`). The measurement
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
        # Real hardware flushes any unread bytes here; model that so a test's
        # "_outbox is empty" assertion checks the driver drained cleanly rather
        # than being masked by a stale queue the flush would have cleared.
        self._outbox.clear()

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
    m._lock = None  # type: ignore[attr-defined]  # built via __new__, never took the port lock
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


def test_value_reply_without_trailing_ok_is_desync() -> None:
    # A value line followed by something other than OK means the stream is
    # desynced — must fail loud, not hand back the value with an unconfirmed frame.
    m = _meter_with({"D": ["-26.45", "ERROR"]})
    with pytest.raises(power_meter.PowerMeterError, match="desynced"):
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


def test_resolve_band_mhz_real_region_names() -> None:
    # The blocking bug the old suite missed: sweep(band=None) feeds through the
    # protobuf region enum name ("US", "EU_868", ...), not "US915"/"EU868".
    assert pa_sweep.resolve_band_mhz("US") == pytest.approx(915.0)  # 902-928 centre
    assert power_meter.nearest_freq_index(pa_sweep.resolve_band_mhz("US")) == 4  # -> 900 MHz curve
    assert pa_sweep.resolve_band_mhz("EU_868") == pytest.approx((869.4 + 869.65) / 2)
    assert power_meter.nearest_freq_index(pa_sweep.resolve_band_mhz("EU_868")) == 3  # -> 868
    assert pa_sweep.resolve_band_mhz("EU_433") == pytest.approx(433.5)
    assert power_meter.nearest_freq_index(pa_sweep.resolve_band_mhz("EU_433")) == 2  # -> 433


def test_resolve_band_mhz_accepts_bare_mhz() -> None:
    assert pa_sweep.resolve_band_mhz("868") == pytest.approx(868.0)


def test_resolve_band_mhz_rejects_garbage() -> None:
    with pytest.raises(pa_sweep.PaSweepError, match="Unknown band"):
        pa_sweep.resolve_band_mhz("NOTABAND")


def test_open_is_exclusive_per_port(monkeypatch) -> None:
    # A second open on the same meter port fails fast with a busy error (the
    # "one call per serial port" rule), and the port frees again after close.
    from meshtastic_mcp import registry

    monkeypatch.setattr(power_meter.serial, "Serial", lambda *_a, **_k: FakeSerial({}))
    registry.clear_port_lock("COM-LOCK-TEST")

    m1 = power_meter.PowerMeter("COM-LOCK-TEST").open()
    try:
        with pytest.raises(power_meter.PowerMeterError, match="busy"):
            power_meter.PowerMeter("COM-LOCK-TEST").open()
    finally:
        m1.close()

    m2 = power_meter.PowerMeter("COM-LOCK-TEST").open()  # freed after close -> succeeds
    m2.close()


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


def test_sample_rejects_nonpositive_count() -> None:
    # measure()/floor capture do min/mean/max on the result — an empty list from
    # count<=0 would crash opaquely downstream, so reject it at the source.
    m = _meter_with({"D": ["-25.0", "OK"]})
    with pytest.raises(power_meter.PowerMeterError, match="count must be >= 1"):
        m.sample(0)


def test_sweep_requires_confirm() -> None:
    with pytest.raises(pa_sweep.PaSweepError, match="confirm=True"):
        pa_sweep.sweep([20], band="EU_868", confirm=False)


# ---------------------------------------------------------------------------
# sweep() duty-cycle-override restore (mocked collaborators, no hardware)
# ---------------------------------------------------------------------------
class _FakeMeter:
    """Stand-in for `PowerMeter` in sweep tests: floor at -25, TX-active at -10."""

    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self) -> _FakeMeter:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def version(self) -> str:
        return "RFPowerMeterv2 1.0.11"

    def set_freq_mhz(self, mhz: float, *, persist: bool = False) -> int:
        return int(mhz)

    def sample(self, count: int, *, interval_s: float = 0.05, peak: bool = False) -> list[float]:
        return [-25.0] * count  # noise floor

    def read_avg_dbm(self) -> float:
        return -10.0  # clears floor+margin => a TX-active sample


def _patch_sweep(monkeypatch, *, original_duty: bool) -> list[tuple[str, object]]:
    """Wire sweep()'s collaborators to fakes; return the recorded set_config calls."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        pa_sweep.admin, "set_config", lambda path, value, port=None: calls.append((path, value))
    )
    monkeypatch.setattr(pa_sweep.admin, "send_text", lambda **_k: {"ok": True, "packet_id": 1})
    monkeypatch.setattr(
        pa_sweep,
        "read_lora_context",
        # tx_power distinct from every swept step (17, 20) so the restore
        # assertion verifies cleanup ran, not just the last step's write.
        lambda port=None: {
            "region": "EU_868",
            "tx_power": 13,
            "override_duty_cycle": original_duty,
        },
    )
    monkeypatch.setattr(pa_sweep.power_meter, "PowerMeter", _FakeMeter)
    monkeypatch.setattr(pa_sweep.time, "sleep", lambda *_a: None)
    return calls


def test_sweep_leaves_duty_override_untouched_when_already_on(monkeypatch) -> None:
    calls = _patch_sweep(monkeypatch, original_duty=True)
    pa_sweep.sweep(
        [17, 20], band="EU_868", confirm=True, settle_s=0, floor_samples=3, burst_repeat=1
    )
    duty_writes = [c for c in calls if c[0] == "lora.override_duty_cycle"]
    assert duty_writes == [], "must not write override_duty_cycle when it was already True"
    # tx_power restored to the original (13), distinct from the last step (20).
    tx_writes = [v for p, v in calls if p == "lora.tx_power"]
    assert tx_writes[-1] == 13


def test_sweep_enables_then_restores_duty_override_when_originally_off(monkeypatch) -> None:
    calls = _patch_sweep(monkeypatch, original_duty=False)
    pa_sweep.sweep(
        [17, 20], band="EU_868", confirm=True, settle_s=0, floor_samples=3, burst_repeat=1
    )
    duty_writes = [v for p, v in calls if p == "lora.override_duty_cycle"]
    assert duty_writes[0] is True, "should enable the override for the bench run"
    assert duty_writes[-1] is False, "should restore to the original (off) value, not force True"


def test_sweep_passes_tuned_tx_linger_to_send_text(monkeypatch) -> None:
    # Bursts must key with the sweep's tuned tx_linger_s, not send_text's 8 s
    # interactive default (which would be paid per burst per step).
    sent: list[dict] = []
    monkeypatch.setattr(pa_sweep.admin, "set_config", lambda path, value, port=None: None)
    monkeypatch.setattr(
        pa_sweep.admin, "send_text", lambda **k: (sent.append(k), {"ok": True, "packet_id": 1})[1]
    )
    monkeypatch.setattr(
        pa_sweep,
        "read_lora_context",
        lambda port=None: {"region": "EU_868", "tx_power": 13, "override_duty_cycle": True},
    )
    monkeypatch.setattr(pa_sweep.power_meter, "PowerMeter", _FakeMeter)
    monkeypatch.setattr(pa_sweep.time, "sleep", lambda *_a: None)

    pa_sweep.sweep(
        [20],
        band="EU_868",
        confirm=True,
        settle_s=0,
        floor_samples=3,
        burst_repeat=2,
        tx_linger_s=3.5,
    )
    assert len(sent) == 2, "one send per burst_repeat"
    assert all(k.get("tx_linger_s") == 3.5 for k in sent), "each burst uses the tuned linger"


def test_sweep_restores_independently_and_reports_errors(monkeypatch) -> None:
    # If the tx_power restore fails (port busy), the duty-cycle restore must STILL
    # run — leaving override_duty_cycle stuck on is the regulatory hazard — and the
    # failure must surface in restore_errors rather than being swallowed.
    calls: list[tuple[str, object]] = []

    def flaky_set_config(path, value, port=None):
        calls.append((path, value))
        if path == "lora.tx_power" and value == 99:  # the restore of the original
            raise RuntimeError("port COM7 is busy - retry shortly")

    monkeypatch.setattr(pa_sweep.admin, "set_config", flaky_set_config)
    monkeypatch.setattr(pa_sweep.admin, "send_text", lambda **_k: {"ok": True, "packet_id": 1})
    monkeypatch.setattr(
        pa_sweep,
        "read_lora_context",
        lambda port=None: {"region": "EU_868", "tx_power": 99, "override_duty_cycle": False},
    )
    monkeypatch.setattr(pa_sweep.power_meter, "PowerMeter", _FakeMeter)
    monkeypatch.setattr(pa_sweep.time, "sleep", lambda *_a: None)

    res = pa_sweep.sweep(
        [20], band="EU_868", confirm=True, settle_s=0, floor_samples=3, burst_repeat=1
    )

    assert res["restore_errors"], "a failed restore must be surfaced"
    assert any("lora.tx_power" in e for e in res["restore_errors"])
    # The duty-cycle restore ran despite the tx_power restore failing.
    assert ("lora.override_duty_cycle", False) in calls
