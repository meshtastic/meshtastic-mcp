# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ImmersionRC RF Power Meter v2 driver — the measurement instrument half of the
PA-calibration bench (`pa_sweep.py` is the closed-loop orchestration half).

This is a lab power meter, not an SDR: it reports a single scalar power reading
(average or peak dBm) through its internal log detector, already calibrated per
frequency band. That makes it the right tool for the one thing the RTL-SDR
oracle (`sdr.py`) deliberately can't do — read *absolute* transmit power off a
Meshtastic node's PA and watch how it tracks the configured `lora.tx_power`
across a sweep. See `IMMERSIONRC_METER_HANDOFF.md` for the hardware notes this
driver is built from (verified live against firmware 1.0.11).

The wire protocol is line-based ASCII over USB CDC; every reply is either
``<value>\\r\\n`` followed by ``OK\\r\\n``, or a bare ``OK\\r\\n`` (for the persist
command), or ``ERROR\\r\\n`` for an unknown command. All the parsing lives in
``PowerMeter._cmd`` and is exercised by `tests/unit/test_power_meter.py` against
a fake serial port — no meter required to test the protocol logic.

Calibration caveat (same spirit as `sdr.py`): the meter is accurate to ~±0.5 dB
and the reading depends on the external attenuator you must add in front of it
(the meter tops out at +30 dBm / 1.3 W absolute max — a bare Meshtastic PA at
+22 dBm needs a pad). Attenuator correction is applied in software (`pa_sweep`),
never sent to the meter. Treat results as a bench regression check, not a
certified measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

# USB CDC identifiers for the ImmersionRC RF Power Meter v2 (Microchip CDC stack).
# This firmware (1.0.11) reports no manufacturer string, so match on VID/PID only;
# newer firmware may add manufacturer == "ImmersionRC" (the ELRS tooling also matches
# on that) but we don't rely on it.
METER_VID = 0x04D8
METER_PID = 0x000A

# The meter's stored per-frequency calibration curves, selected by 0-based index
# via the ``F<idx>`` command. Values are MHz. Verified against fw 1.0.11 — note
# there is no dedicated 915 MHz point, so US915 work uses the 900 MHz curve (F4),
# matching the ExpressLRS tooling convention.
FREQ_INDEX_MHZ: tuple[int, ...] = (
    35, 72, 433, 868, 900, 1200, 2400,
    5600, 5650, 5700, 5750, 5800, 5850, 5900, 5950, 6000,
)  # fmt: skip

# Meshtastic band -> the nearest usable calibration point on this meter.
# EU868 lands exactly on F3; US915 uses F4 (900 MHz), the closest stored curve.
_MESHTASTIC_BAND_MHZ: dict[str, int] = {
    "EU868": 868,
    "US915": 900,
    "ANZ": 900,
    "LORA_24": 2400,
}

# Reference tools send ``F<idx>`` twice with ~200 ms spacing "for reliability";
# the second write reliably takes even when the first is dropped mid-enumeration.
_FREQ_SET_REPEAT_GAP_S = 0.2

# Absolute-max input the meter tolerates (1.3 W); readings above +30 dBm are out
# of spec and >30 s above +27 dBm can damage it. `pa_sweep` refuses to key TX
# that would exceed this after attenuator correction.
METER_ABS_MAX_DBM = 31.0


class PowerMeterError(RuntimeError):
    """Raised for a missing meter, a serial failure, or an unexpected reply."""


def list_meters() -> list[str]:
    """Return the device paths of all attached ImmersionRC meters (matched by VID/PID).

    Never opens a port — pure enumeration, safe to call from capability
    detection. Returns ``[]`` (never raises) when pyserial can't enumerate.
    """
    try:
        return [
            p.device for p in list_ports.comports() if p.vid == METER_VID and p.pid == METER_PID
        ]
    except Exception:
        # Capability detection must never crash startup — a wedged USB stack or
        # a platform quirk in comports() just means "no meter".
        return []


def find_meter_port() -> str | None:
    """First attached meter's device path, or None. See `list_meters`."""
    meters = list_meters()
    return meters[0] if meters else None


def nearest_freq_index(mhz: float) -> int:
    """Index into `FREQ_INDEX_MHZ` of the calibration point closest to `mhz`.

    The meter has no continuous tuning — it applies whichever stored curve is
    active — so any target frequency snaps to the nearest available point.
    """
    return min(range(len(FREQ_INDEX_MHZ)), key=lambda i: abs(FREQ_INDEX_MHZ[i] - mhz))


def band_to_freq_mhz(band: str) -> int:
    """Resolve a band name (``"EU868"``/``"US915"``/...) or a numeric string to MHz.

    Accepts a Meshtastic region label or a bare MHz value (``"868"``) so callers
    can pass either a region or an explicit frequency.
    """
    key = band.strip().upper()
    if key in _MESHTASTIC_BAND_MHZ:
        return _MESHTASTIC_BAND_MHZ[key]
    try:
        return int(float(band))
    except ValueError as exc:
        raise PowerMeterError(
            f"Unknown band {band!r}. Use a MHz value or one of: {', '.join(_MESHTASTIC_BAND_MHZ)}."
        ) from exc


@dataclass(frozen=True)
class MeterInfo:
    port: str
    version: str
    stored_freq_mhz: int


class PowerMeter:
    """Synchronous line-protocol client for the ImmersionRC RF Power Meter v2.

    Usage::

        with PowerMeter(port) as m:
            m.set_freq_mhz(868)          # activate the 868 MHz calibration curve
            avg = m.read_avg_dbm()       # D — average power
            peak = m.read_peak_dbm()     # E — peak power

    One meter, one open connection: the auto-off timeout re-enumerates the USB
    device, so hold a single connection for the whole measurement rather than
    reopening per reading (reopening races the enumeration). `pa_sweep` follows
    this — it opens the meter once and keeps it for the full sweep.
    """

    def __init__(self, port: str | None = None, baud: int = 9600, timeout: float = 0.5) -> None:
        self._port = port or find_meter_port()
        if self._port is None:
            raise PowerMeterError(
                "No ImmersionRC power meter found (VID 0x04D8 / PID 0x000A). "
                "Plug it in and confirm it is powered on — it auto-powers-off on "
                "a battery timeout and disappears from USB when it does."
            )
        self._baud = baud
        self._timeout = timeout
        self._ser: serial.Serial | None = None

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> PowerMeter:
        if self._ser is not None:
            return self
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=self._timeout)
        except serial.SerialException as exc:
            raise PowerMeterError(f"Could not open power meter at {self._port}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> PowerMeter:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def port(self) -> str:
        assert self._port is not None  # guaranteed non-None by __init__
        return self._port

    # -- protocol -----------------------------------------------------------
    def _cmd(self, command: str) -> str:
        """Send one command, return its value token (or ``"OK"`` for `S`).

        Reply grammar (baud is irrelevant on CDC; commands are case-insensitive):
            value command -> ``<value>\\r\\n`` then ``OK\\r\\n``  -> returns ``<value>``
            persist (`S`) -> ``OK\\r\\n``                          -> returns ``"OK"``
            unknown       -> ``ERROR\\r\\n``                       -> raises
        """
        if self._ser is None:
            raise PowerMeterError("power meter is not open; call open() or use a with-block")
        try:
            self._ser.reset_input_buffer()
            self._ser.write((command + "\n").encode("ascii"))
            first = self._ser.readline().decode("ascii", "replace").strip()
            if first == "":
                raise PowerMeterError(
                    f"no reply to {command!r} — meter may have powered off (auto-off timeout) "
                    "or the connection dropped"
                )
            if first == "ERROR":
                raise PowerMeterError(f"meter rejected command {command!r} (ERROR)")
            if first == "OK":
                return "OK"
            # Value replies are followed by a trailing OK line; consume AND validate
            # it. If it isn't OK (a dropped byte, an out-of-band ERROR, a truncated
            # frame) the stream is desynced — fail loud rather than hand back a value
            # paired with an unconfirmed reply and let the next command read garbage.
            trailer = self._ser.readline().decode("ascii", "replace").strip()
            if trailer != "OK":
                raise PowerMeterError(
                    f"desynced reply to {command!r}: expected trailing 'OK' after value "
                    f"{first!r}, got {trailer!r}"
                )
            return first
        except serial.SerialException as exc:
            raise PowerMeterError(
                f"serial error talking to meter at {self._port}: {exc} "
                "(the meter re-enumerates on auto-off — re-scan for VID/PID)"
            ) from exc

    def _cmd_float(self, command: str) -> float:
        raw = self._cmd(command)
        try:
            return float(raw)
        except ValueError as exc:
            raise PowerMeterError(f"expected a number from {command!r}, got {raw!r}") from exc

    # -- high-level operations ---------------------------------------------
    def version(self) -> str:
        """Firmware version string, e.g. ``RFPowerMeterv2 1.0.11`` (the ``V`` command)."""
        v = self._cmd("V")
        if "RFPowerMeter" not in v:
            raise PowerMeterError(f"unexpected version reply {v!r} — is this an ImmersionRC meter?")
        return v

    def read_avg_dbm(self) -> float:
        """Current **average** power in dBm (the ``D`` command). Primary reading for
        constant-envelope LoRa bursts."""
        return self._cmd_float("D")

    def read_peak_dbm(self) -> float:
        """Current **peak** power in dBm (the ``E`` command). Cross-check for `read_avg_dbm`."""
        return self._cmd_float("E")

    def stored_freq_mhz(self) -> int:
        """The persisted calibration frequency shown on the LCD (the bare ``F`` query).

        Note the quirk: a serial ``F<idx>`` changes the *active* curve immediately
        but does NOT update this stored value until an ``S`` (persist) — so right
        after `set_freq_mhz(persist=False)` this still reports the old value even
        though readings already reflect the new curve.
        """
        return int(self._cmd_float("F"))

    def set_freq_index(self, index: int, *, persist: bool = False) -> int:
        """Activate calibration curve `index` (0-based into `FREQ_INDEX_MHZ`).

        Sent twice with a short gap, matching the reference tooling. Returns the
        MHz the meter echoes back. With ``persist=True`` also issues ``S`` so the
        LCD updates and the setting survives a power cycle.
        """
        if not 0 <= index < len(FREQ_INDEX_MHZ):
            raise PowerMeterError(
                f"frequency index {index} out of range 0..{len(FREQ_INDEX_MHZ) - 1}"
            )
        self._cmd(f"F{index}")
        time.sleep(_FREQ_SET_REPEAT_GAP_S)
        echoed = self._cmd(f"F{index}")
        if persist:
            self._cmd("S")
        try:
            return int(float(echoed))
        except ValueError:
            return FREQ_INDEX_MHZ[index]

    def set_freq_mhz(self, mhz: float, *, persist: bool = False) -> int:
        """Activate the calibration curve nearest to `mhz`. See `set_freq_index`."""
        return self.set_freq_index(nearest_freq_index(mhz), persist=persist)

    def info(self) -> MeterInfo:
        """One round-trip snapshot: version + stored frequency."""
        return MeterInfo(self.port, self.version(), self.stored_freq_mhz())

    def sample(self, count: int, *, interval_s: float = 0.05, peak: bool = False) -> list[float]:
        """Take `count` readings, `interval_s` apart, returning the dBm values.

        Uses ``D`` (average) by default, ``E`` (peak) when ``peak=True``. ~10-12 Hz
        (interval 0.05 s) is the sustained rate the meter handles comfortably.
        """
        if count <= 0:
            raise PowerMeterError(f"sample count must be >= 1, got {count}")
        read = self.read_peak_dbm if peak else self.read_avg_dbm
        out: list[float] = []
        for i in range(count):
            out.append(read())
            if i + 1 < count:
                time.sleep(interval_s)
        return out
