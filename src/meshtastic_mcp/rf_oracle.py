# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""RF compliance oracle: cross-checks `lora_compliance` (prediction, from a live
device's *configured* LoRa settings) against `sdr` (measurement, from an
RTL-SDR capture) to answer "is the radio actually doing what its own config
says it should" — independent of the device's self-reported packet log.

The headline case this catches that nothing else in this repo can: firmware
reports a packet sent (`send_text` returns ok) but no RF actually left the
antenna — a disconnected antenna, a dead PA, a region/frequency
miscalculation. `confirm_tx()` answers that by capturing IQ centered on the
*predicted* frequency spanning the `send_text()` call and checking whether
anything showed up on air at all, plus whether what did show up looks like
it's in the right place (frequency, bandwidth, region).

Note what this can't do: the recorder's packet log is NOT a usable
cross-check for a self-originated broadcast. `meshtastic.MeshInterface`
explicitly discards the local echo of a packet you just sent (firmware omits
the redundant `from` field on that echo; the library detects the missing
field and drops it rather than publishing a pubsub event) — see
`confirm_tx`'s docstring. So `firmware_self_reported_tx` in the returned dict
will be `False` for essentially every unacked broadcast regardless of whether
the send worked; `measured.rf_observed` (the actual SDR evidence) is the
signal to trust. A *second* Meshtastic device on the same channel, checked via
its own recorder, is the real independent cross-check for that case — its
receive event is never suppressed since `from` is genuinely populated there.

This is a coarse dev-loop regression check, not a certified compliance
measurement — see `sdr.py`'s module docstring for the calibration caveat.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
from meshtastic.protobuf import channel_pb2

from . import lora_compliance, sdr
from .admin import _message_to_dict
from .admin import send_text as _admin_send_text
from .connection import connect
from .log_query import packets_window


class RfOracleError(RuntimeError):
    pass


# Typical RTL2832U + R820T/R820T2 tuner range. Caught here for a clear error
# instead of an opaque librtlsdr failure deep inside capture_iq().
_RTL_SDR_MIN_HZ = 24e6
_RTL_SDR_MAX_HZ = 1_766e6

_MAX_SAMPLE_RATE_HZ = 2_400_000.0  # RTL2832U's realistic sustained ceiling
# Empirically, this NooElec/R820T2 unit throws LIBUSB_ERROR_OVERFLOW on the very first read
# after retuning at sample rates near the bottom of librtlsdr's valid range (900kHz-~1.4MHz
# reproduced; 2.048Msps reliable) — a known class of RTL-SDR/USB-controller flakiness at low
# rates, not specific to this code. Every reachable Meshtastic preset (LORA_24/wideLora is the
# only >500kHz case, and it's at 2.4GHz — already unreachable, see `_check_tunable`) fits
# comfortably above this floor, so there's no reason for our own rate selection to go lower.
_MIN_SAFE_SAMPLE_RATE_HZ = 2_048_000.0


def read_lora_context(port: str | None = None) -> dict[str, Any]:
    """One `connect()` round-trip: everything `lora_compliance.predict_lora_params`
    needs, read straight off a live device — configured LoRa settings, device
    role (for the EU_866 duty-cycle split), and the primary channel's name
    (empty string if unset, matching firmware's own default-name fallback).
    """
    with connect(port=port) as iface:
        node = iface.localNode
        lora = _message_to_dict(node.localConfig.lora)
        device = _message_to_dict(node.localConfig.device)
        channels = list(node.channels or [])
        primary = next((c for c in channels if c.role == channel_pb2.Channel.Role.PRIMARY), None)
        channel_name = primary.settings.name if primary is not None else ""

    return {
        "region": lora.get("region", "UNSET"),
        "modem_preset": lora.get("modem_preset", "LONG_FAST"),
        "use_preset": lora.get("use_preset", True),
        "bandwidth": lora.get("bandwidth") or None,
        "spread_factor": lora.get("spread_factor") or None,
        "coding_rate": lora.get("coding_rate") or None,
        "channel_num": lora.get("channel_num", 0),
        "override_frequency": lora.get("override_frequency", 0.0),
        "frequency_offset": lora.get("frequency_offset", 0.0),
        "tx_power": lora.get("tx_power", 0),
        "device_role": device.get("role", "CLIENT"),
        "channel_name": channel_name,
    }


def _predict_from_context(ctx: dict[str, Any]) -> lora_compliance.PredictedRf:
    return lora_compliance.predict_lora_params(
        ctx["region"],
        ctx["modem_preset"],
        channel_name=ctx["channel_name"],
        channel_num=ctx["channel_num"],
        use_preset=ctx["use_preset"],
        bandwidth_khz=ctx["bandwidth"],
        spread_factor=ctx["spread_factor"],
        coding_rate=ctx["coding_rate"],
        override_frequency_mhz=ctx["override_frequency"],
        frequency_offset_mhz=ctx["frequency_offset"],
        device_role=ctx["device_role"],
    )


def _check_tunable(freq_mhz: float) -> None:
    hz = freq_mhz * 1e6
    if not (_RTL_SDR_MIN_HZ <= hz <= _RTL_SDR_MAX_HZ):
        raise RfOracleError(
            f"Predicted frequency {freq_mhz:.3f} MHz is outside the RTL-SDR's typical tuner "
            f"range ({_RTL_SDR_MIN_HZ / 1e6:.0f}-{_RTL_SDR_MAX_HZ / 1e6:.0f} MHz, R820T/R820T2) "
            "— this region/preset can't be checked with an RTL-SDR. (LORA_24's 2.4GHz band "
            "needs a different SDR.)"
        )


def _sample_rate_for_bw(bw_khz: float, requested_hz: float) -> float:
    """Bump the sample rate up if it's too narrow to comfortably see the
    predicted occupied bandwidth, capped at the RTL2832U's realistic ceiling.
    """
    minimum = max(bw_khz * 1000.0 * 1.5, _MIN_SAFE_SAMPLE_RATE_HZ)
    if requested_hz >= minimum:
        return requested_hz
    return min(_MAX_SAMPLE_RATE_HZ, minimum)


def _distance_to_window(window: sdr.ActiveWindow, offset_s: float) -> float:
    if window.start_s <= offset_s <= window.end_s:
        return 0.0
    return min(abs(window.start_s - offset_s), abs(window.end_s - offset_s))


def _min_plausible_burst_s(sf: int, bw_khz: float, *, min_preamble_symbols: int = 8) -> float:
    """Floor duration below which an "active window" is noise, not a LoRa packet.

    A LoRa symbol takes `2^sf / bw_hz` seconds; even the shortest real packet has
    at least a several-symbol preamble before payload. High-gain RTL-SDR captures
    reliably produce a forest of sub-millisecond noise spikes that clear a naive
    power threshold (observed directly: ~40 spikes of ~0.125ms each in one 8s
    capture) — those can't be LoRa (a single symbol alone is already tens of ms
    at typical SF/BW), so filter them out before picking a match rather than
    risk matching a noise spike that happens to land closer in time than the
    real, much-longer burst.
    """
    symbol_time_s = (2**sf) / (bw_khz * 1000.0)
    return symbol_time_s * min_preamble_symbols


def _closest_window(
    windows: list[sdr.ActiveWindow],
    offset_s: float,
    *,
    max_distance_s: float,
    min_duration_s: float = 0.0,
) -> sdr.ActiveWindow | None:
    """Pick the active window most likely to *be* the transmission triggered at
    `offset_s` (seconds into the capture) — not just any RF activity in the
    same multi-second window, which on a live mesh is often someone else's
    packet (or, at high gain, electrical noise — see `_min_plausible_burst_s`).
    `None` if nothing plausible is within `max_distance_s` of the send.
    """
    candidates = [w for w in windows if w.duration_s >= min_duration_s]
    if not candidates:
        return None
    best = min(candidates, key=lambda w: _distance_to_window(w, offset_s))
    return best if _distance_to_window(best, offset_s) <= max_distance_s else None


def _slice_window(
    iq: np.ndarray, sample_rate_hz: float, window: sdr.ActiveWindow, *, pad_s: float = 0.05
) -> np.ndarray:
    """IQ samples for one active window, with a small pad on each side (the
    window-power detector's boundaries are coarse — `win_samples` granularity).
    """
    total_s = len(iq) / sample_rate_hz
    start = max(0.0, window.start_s - pad_s)
    end = min(total_s, window.end_s + pad_s)
    return iq[int(start * sample_rate_hz) : int(end * sample_rate_hz)]


def scan(
    center_freq_hz: float,
    *,
    span_khz: float = 1000.0,
    duration_s: float = 2.0,
    gain: float | str = "auto",
    device_index: int = 0,
    db_down: float = 26.0,
    active_threshold_db: float = 10.0,
) -> dict[str, Any]:
    """Capture and characterize RF activity at a frequency — no Meshtastic
    device involved. Useful for a pre-test "is this channel already busy"
    occupancy check, or just probing a frequency by hand.
    """
    requested_rate_hz = min(
        _MAX_SAMPLE_RATE_HZ, max(span_khz * 1000.0 * 1.5, _MIN_SAFE_SAMPLE_RATE_HZ)
    )
    iq, _t_start, _t_end, sample_rate_hz = sdr.capture_iq_timed(
        center_freq_hz, requested_rate_hz, duration_s, gain=gain, device_index=device_index
    )

    _freqs, psd_db = sdr.power_spectrum_db(iq, sample_rate_hz)
    windows = sdr.active_windows(iq, sample_rate_hz, threshold_db=active_threshold_db)
    ob = sdr.occupied_bandwidth(iq, sample_rate_hz, db_down=db_down) if windows else None

    return {
        "center_hz": center_freq_hz,
        "sample_rate_hz": sample_rate_hz,
        "duration_s": duration_s,
        "active_windows": [{"start_s": w.start_s, "duration_s": w.duration_s} for w in windows],
        "duty_cycle_pct_in_capture": round(sdr.duty_cycle_pct(windows, duration_s), 3),
        "occupied_bandwidth_hz": ob.bw_hz if ob else None,
        "peak_freq_offset_hz": ob.peak_freq_hz if ob else None,
        "peak_power_db": float(np.max(psd_db)),
        "noise_floor_db_estimate": float(np.median(psd_db)),
    }


def confirm_tx(
    text: str,
    *,
    channel_index: int = 0,
    port: str | None = None,
    window_s: float = 5.0,
    pre_delay_s: float = 0.4,
    sample_rate_hz: float = 2_048_000.0,
    gain: float | str = "auto",
    device_index: int = 0,
    db_down: float = 26.0,
    active_threshold_db: float = 10.0,
    band_guard_khz: float = 100.0,
    tx_confirm_lookback_s: float = 60.0,
) -> dict[str, Any]:
    """Send a text message while an RTL-SDR captures the predicted frequency,
    and cross-check what actually showed up on air against the firmware's own
    configured LoRa settings (via `lora_compliance`).

    Timing is loose (capture starts in a background thread, `pre_delay_s`
    later `send_text` fires on the main thread) — fine for "did energy show up
    somewhere in this multi-second window", not for measuring a single
    packet's exact airtime. Capture window must comfortably exceed the
    preset's expected on-air time (a LONG_SLOW packet can take several
    seconds) or the transmission may start before/finish after the window.

    **Queued-but-delayed TX is real and can look identical to a dropped send.**
    Firmware enforces its own channel-utilization/airtime budget: under heavy
    recent traffic (including your own rapid-fire test calls saturating that
    budget), a queued packet can sit for tens of seconds before actually being
    keyed — empirically observed here as a 40-70s delay after back-to-back test
    sends on a single node. Neither this function's SDR capture (bounded by
    `window_s`, since holding the SDR open is the expensive part) nor a naive
    short recorder poll will see a transmission that happens after they've
    already stopped watching. `ok=False`/`silent_tx_suspected=True` from a
    *single* short-window call is NOT conclusive evidence of a real failure if
    you've been calling `send_text`/`confirm_tx` repeatedly in quick
    succession — space calls out, or re-check with `rf_scan` a bit later,
    before concluding TX is actually broken.

    `ok`/`measured.rf_observed` (independent SDR evidence, bounded by
    `window_s`) is the primary verdict here, with the delayed-TX caveat above.
    `firmware_self_reported_tx` is a secondary, best-effort signal with its own
    limitation: `meshtastic.MeshInterface._handlePacketFromRadio` explicitly
    discards and never publishes a pubsub event for a packet that echoes back
    your own node's send (firmware omits the now-redundant `from` field to
    save bytes on that echo; the library detects the missing field and drops
    it — "Device returned a packet we sent, ignoring"). So for a plain
    broadcast with `want_ack=False`, `firmware_self_reported_tx` will be
    `False` almost always regardless of whether the send worked — it only has
    a chance of being `True` if some other node relays/ACKs the packet back to
    us with the relay's own `from` populated. To make this check actually
    useful despite the delayed-TX behavior above, its packet-log lookback
    (`tx_confirm_lookback_s`, default 60s) is intentionally decoupled from and
    much longer than the live SDR `window_s` — checking the log is cheap, so
    there's no reason to bound it as tightly as the RF capture. For a real
    independent cross-node oracle, have a *second* Meshtastic device on the
    same channel and check its recorder for a genuine receive event instead
    (see the `meshtastic-e2e` skill's closed-loop pattern) — that receive
    event is never suppressed since `from` is legitimately populated on a
    genuinely different node's receive, and the same delayed-TX caveat still
    applies (watch long enough).
    """
    ctx = read_lora_context(port=port)
    pred = _predict_from_context(ctx)
    _check_tunable(pred.freq_mhz)

    center_hz = pred.freq_mhz * 1e6
    requested_rate_hz = _sample_rate_for_bw(pred.bw_khz, sample_rate_hz)

    capture_result: dict[str, Any] = {}

    def _capture() -> None:
        try:
            iq, t_start, t_end, actual_rate = sdr.capture_iq_timed(
                center_hz, requested_rate_hz, window_s, gain=gain, device_index=device_index
            )
            capture_result["iq"] = iq
            capture_result["t_start"] = t_start
            capture_result["t_end"] = t_end
            capture_result["sample_rate_hz"] = actual_rate
        except sdr.SdrError as exc:
            capture_result["error"] = str(exc)

    capture_thread = threading.Thread(target=_capture, daemon=True)
    capture_thread.start()
    time.sleep(pre_delay_s)

    t_send = time.monotonic()
    send_result = _admin_send_text(text=text, channel_index=channel_index, port=port)

    capture_thread.join(timeout=window_s + 15.0)
    if capture_thread.is_alive():
        raise RfOracleError("SDR capture thread did not finish in time — device may be wedged")
    if "error" in capture_result:
        raise RfOracleError(f"SDR capture failed: {capture_result['error']}")
    iq = capture_result.get("iq")
    if iq is None:
        raise RfOracleError("SDR capture produced no samples")
    t_start = capture_result["t_start"]
    sample_rate_hz = capture_result["sample_rate_hz"]
    send_offset_s = t_send - t_start

    # All RF activity seen in the capture (ambient mesh traffic included) — useful context,
    # but NOT what "rf_observed" means below: that's specifically the window matched to our send.
    windows = sdr.active_windows(iq, sample_rate_hz, threshold_db=active_threshold_db)
    duty_pct = sdr.duty_cycle_pct(windows, observation_window_s=window_s)

    # Match the window closest to our actual send_text() call — analyzing only that slice
    # instead of averaging the whole capture avoids blurring our packet together with any
    # other traffic sharing the channel during the window. Tolerance is generous (LoRa
    # packets can take seconds of airtime at high SF) but capped so an unrelated burst much
    # later/earlier in the capture doesn't get misattributed to this send.
    match_tolerance_s = max(1.0, min(window_s / 2.0, 3.0))
    min_burst_s = _min_plausible_burst_s(pred.sf, pred.bw_khz)
    matched = _closest_window(
        windows, send_offset_s, max_distance_s=match_tolerance_s, min_duration_s=min_burst_s
    )

    region_info = lora_compliance.REGIONS[ctx["region"]]
    guard_hz = band_guard_khz * 1000.0
    ob = None
    in_band_frac = None
    if matched is not None:
        matched_iq = _slice_window(iq, sample_rate_hz, matched)
        ob = sdr.occupied_bandwidth(matched_iq, sample_rate_hz, db_down=db_down)
        in_band_frac = sdr.in_band_fraction(
            matched_iq,
            sample_rate_hz,
            capture_center_hz=center_hz,
            band_start_hz=region_info.freq_start_mhz * 1e6 - guard_hz,
            band_end_hz=region_info.freq_end_mhz * 1e6 + guard_hz,
        )

    # Best-effort only — see docstring. This is `False` for essentially every unacked
    # broadcast regardless of whether the send worked (the library discards the local echo
    # of your own packet before it ever reaches the recorder), so it can only ever go `True`
    # via some other node's relay/ACK bouncing the packet_id back to us. Not the primary signal.
    # Lookback is deliberately much longer than `window_s` — queued packets under airtime
    # pressure can transmit tens of seconds late (see docstring); checking the log further
    # back costs nothing, unlike extending the live SDR capture would.
    pkt_window = packets_window(
        start=f"-{tx_confirm_lookback_s:.0f}s", portnum="TEXT_MESSAGE_APP", max=20
    )
    packet_id = send_result.get("packet_id")
    firmware_self_reported_tx = any(
        packet_id is not None and pkt.get("id") == packet_id
        for pkt in pkt_window.get("packets", [])
    )

    rf_observed = matched is not None
    # The actual "silent TX" signal: firmware said the send was queued OK, but no RF showed up
    # anywhere near the predicted frequency during the capture window.
    silent_tx_suspected = bool(send_result.get("ok")) and not rf_observed

    return {
        "ok": rf_observed,
        "silent_tx_suspected": silent_tx_suspected,
        "predicted": {
            "region": pred.region,
            "freq_mhz": round(pred.freq_mhz, 4),
            "bw_khz": pred.bw_khz,
            "sf": pred.sf,
            "cr": pred.cr,
            "duty_cycle_limit_pct": pred.duty_cycle_pct,
            "power_limit_dbm": pred.power_limit_dbm,
        },
        "measured": {
            "rf_observed": rf_observed,
            "matched_window": (
                {"start_s": matched.start_s, "duration_s": matched.duration_s} if matched else None
            ),
            "occupied_bandwidth_hz": ob.bw_hz if ob else None,
            "freq_offset_from_predicted_hz": ob.peak_freq_hz if ob else None,
            "in_region_band_fraction": round(in_band_frac, 4) if in_band_frac is not None else None,
            "all_active_windows_in_capture": [
                {"start_s": w.start_s, "duration_s": w.duration_s} for w in windows
            ],
            "duty_cycle_pct_in_capture": round(duty_pct, 3),
        },
        "firmware_self_reported_tx": firmware_self_reported_tx,
        "send_result": send_result,
        "capture": {
            "center_hz": center_hz,
            "sample_rate_hz": sample_rate_hz,
            "window_s": window_s,
            "pre_delay_s": pre_delay_s,
            "send_offset_s": round(send_offset_s, 4),
            "tx_confirm_lookback_s": tx_confirm_lookback_s,
        },
        "caveat": (
            "RTL-SDR dev-loop regression check, uncalibrated power reference — "
            "not a substitute for certified EMC-lab compliance testing."
        ),
    }
