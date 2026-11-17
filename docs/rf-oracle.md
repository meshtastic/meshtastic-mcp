# RF oracle: on-air checks with an RTL-SDR

The `sdr` capability (the `[sdr]` extra, librtlsdr and an attached RTL-SDR) registers two tools that check what a radio actually puts on the air, independent of anything the device reports about itself. `rf_oracle.py` holds the logic; the prediction comes from `lora_compliance` and the measurement from `sdr`.

## `rf_scan`

Captures and characterizes RF activity at `center_freq_hz` over `span_khz` for `duration_s`. No Meshtastic device is involved: use it for a pre-test occupancy check ("is this band already busy?") or to probe a frequency by hand.

Returns `{center_hz, sample_rate_hz, duration_s, active_windows: [{start_s, duration_s}], duty_cycle_pct_in_capture, occupied_bandwidth_hz, peak_freq_offset_hz, peak_power_db, noise_floor_db_estimate}`.

## `rf_confirm_tx`

Sends a text message while the SDR captures the frequency the device's own configured region, preset and channel predict it should transmit on. That is ground truth, not the device's self-reported packet log.

It catches the bug class firmware can never report: a TX reported as queued (`send_result.ok`) with no RF leaving the antenna, from a dead PA, a disconnected antenna, or a region or frequency miscalculation. It also flags off-frequency or off-region emission, and reports the measured occupied bandwidth beside the region's regulatory duty-cycle and power limits.

Read the result this way:

- `ok` and `measured.rf_observed` are the SDR evidence, and `silent_tx_suspected` rests on them alone.
- **A delayed TX looks like a dropped one.** Firmware enforces its own channel-utilization budget, so after a lot of recent traffic from you or the mesh a queued packet can wait tens of seconds before it transmits; 40 to 70 s delays have been observed after back-to-back test calls. One `ok=False` / `silent_tx_suspected=True` right after a burst of sends is not conclusive. Space calls out, or recheck with `rf_scan` shortly after, before concluding TX is broken.
- `firmware_self_reported_tx` is best effort, not a cross-check. The `meshtastic` library discards the local echo of a packet you just sent (firmware omits the redundant `from` on that echo, and the library drops a packet without it instead of publishing it), so for a plain broadcast it is `False` almost always, whether or not the send worked. `tx_confirm_lookback_s` (default 60 s) sets how far back that check searches the packet log, longer than `window_s` because reading the log is cheap and extending the live capture is not.
- For a real cross-node check, put a second Meshtastic device on the same channel and read its recorder: its receive event is never suppressed, because `from` is populated there. The delayed-TX caveat still applies, so watch long enough.

Coverage is the R820T/R820T2 tuner range, about 24 to 1766 MHz, so `LORA_24` (2.4 GHz) cannot be checked. The power reference is uncalibrated: this is a dev-loop regression check, not a substitute for certified EMC-lab compliance testing. `doctor` shows whether the capability is active.

Returns `{ok, silent_tx_suspected, predicted: {region, freq_mhz, bw_khz, sf, cr, duty_cycle_limit_pct, power_limit_dbm}, measured: {rf_observed, matched_window, occupied_bandwidth_hz, freq_offset_from_predicted_hz, in_region_band_fraction, all_active_windows_in_capture, duty_cycle_pct_in_capture}, firmware_self_reported_tx, send_result, capture, caveat}`.

For absolute TX power rather than presence and frequency, see the power meter in [power-meter.md](power-meter.md).
