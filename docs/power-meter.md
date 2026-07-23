# RF power meter — ImmersionRC RF Power Meter v2

The `power_meter` capability drives an **ImmersionRC RF Power Meter v2** over USB
to measure the absolute transmit power coming off a Meshtastic node's PA. It is
the measurement instrument behind the PA-calibration bench: `power_meter.py` is
the driver, `pa_sweep.py` is the closed-loop orchestration, and three MCP tools
expose them — `pa_meter_status`, `pa_measure`, and `pa_sweep`.

This complements the RTL-SDR RF-compliance oracle (`sdr.py` / `rf_oracle.py`).
The SDR answers *"did RF leave the antenna, at the right frequency, in-band?"*;
the meter answers *"how much power, in absolute dBm, and where does the PA stop
tracking the configured `lora.tx_power`?"* — a calibrated scalar the uncalibrated
SDR cannot provide.

The driver needs no extra dependency: it is pure `pyserial` (already a core dep).
Everything below was verified live against meter firmware **1.0.11**.

## Device identity

- USB CDC serial device, **VID `0x04D8` (Microchip) / PID `0x000A`**. Windows
  shows it as "Serielles USB-Gerät (COMx)". This firmware reports no manufacturer
  string, so detection matches on VID/PID only (`power_meter.list_meters()`).
- Baud rate is irrelevant on CDC; the driver opens at 9600.
- **Auto-off:** the meter runs on a battery and powers itself off on an idle
  timeout. When it does, the USB device disappears entirely and re-enumerates on
  power-up (usually — but not guaranteed — on the same COM port). Treat a
  vanished port as "asleep" and re-scan for the VID/PID. Hold **one** connection
  for a whole measurement rather than reopening per reading, which races the
  re-enumeration.

## Wire protocol

Line-based ASCII, commands terminated with `\n`, case-insensitive. Every reply is
either a value line followed by an `OK` line, a bare `OK` line, or `ERROR`:

```
<value>\r\n
OK\r\n
```

| Command | Meaning | Example reply |
|---------|---------|---------------|
| `V`       | firmware version string             | `RFPowerMeterv2 1.0.11` |
| `D`       | current **average** power (dBm)      | `-26.450439` |
| `E`       | current **peak** power (dBm)         | `-25.9596` |
| `F`       | query the **stored** frequency (MHz) | `35` |
| `F<idx>`  | set the active calibration curve by index; echoes the new MHz | `900` |
| `S`       | persist current settings (updates the LCD + stored config) | `OK` only |

The driver validates the trailing `OK` after every value reply; anything else is
treated as a desynced stream and raised, rather than returning an unconfirmed
value that would let the next command read garbage.

## Calibration frequency table

`F<idx>` selects one of 16 stored per-frequency calibration curves (0-based):

```
index:  0    1    2     3     4     5      6      7 ...                              15
MHz:   35    72   433   868   900   1200   2400   5600 5650 5700 5750 5800 5850 5900 5950 6000
```

The meter has no continuous tuning — it applies whichever stored curve is active
— so any target frequency is snapped to the nearest stored point
(`power_meter.nearest_freq_index`). There is **no dedicated 915 MHz point** on fw
1.0.11: US915-class work uses the 900 MHz curve (`F4`), matching the ExpressLRS
tooling convention.

Region-to-frequency resolution lives in `pa_sweep.resolve_band_mhz`, which maps a
Meshtastic region enum name (`"US"`, `"EU_868"`, `"EU_433"`, `"JP"`, ...) to the
centre of that region's allocation via `lora_compliance.REGIONS` — the same table
firmware derives its channels from — and then snaps to the nearest calibration
point. A bare MHz value (`"868"`) is accepted too. The driver itself knows nothing
about regions; it speaks MHz only.

## Quirks

1. `F<idx>` changes the **active calibration curve immediately** but does **not**
   update the LCD, and a subsequent bare `F` query still returns the old stored
   value until an `S` (persist) is issued. Proof: the no-input noise-floor reading
   shifts with the set curve (35 MHz: −26.5, 6 GHz: −23.5, 900 MHz: −25.3 dBm).
2. `S` persists the serial-set frequency: after `F4` + `S` the LCD shows 900 MHz
   and it survives a power cycle.
3. Reference tools (ExpressLRS) send `F<idx>` **twice** with ~200 ms spacing "for
   reliability"; the driver follows the same convention.
4. Attenuator offsets are **never** sent to the meter — they are applied in
   software (`pa_sweep`), added back on top of the reading.
5. A sustained polling rate of ~10–12 Hz (50 ms between commands) works fine.

## Measurement notes and safety

- **Range:** −20 to +30 dBm (30 dB internal attenuator). **Absolute max +31 dBm
  (1.3 W)**; more than 30 s above +27 dBm is out of spec and can damage the meter.
  Accuracy ±0.5 dB. A bare Meshtastic PA at +22 dBm needs an external pad — pick
  `attenuator_db` so the highest configured step minus the pad stays under the
  absolute max. `pa_sweep` refuses to run a sweep that would exceed it.
- **Noise floor** reads around −25 dBm at the 868/900 MHz calibration — that is
  the log-detector floor, not zero. TX-vs-floor discrimination thresholds samples
  at floor + margin (default 5 dB).
- For LoRa bursts (constant envelope, seconds long) the **average** reading (`D`)
  is the primary number; **peak** (`E`) is a cross-check.
- The attenuator correction is applied to *every* reading, so it also inflates a
  raw noise-floor read by the pad value — pass `attenuator_db=0` when you want the
  meter's unreferenced floor.
- **Meshtastic bench notes:** an idle node transmits rarely, so `pa_sweep` queues
  large (~200 B) text broadcasts for multi-second airtime bursts. On EU_868 it
  overrides `lora.override_duty_cycle` for the run and restores the original value
  afterwards. A `lora.tx_power` change applies live on firmware 2.8.0+; use
  `reboot_between_steps=True` for older firmware that only applies LoRa config on
  reboot.
- **TX linger tuning:** each broadcast holds the node's port open `tx_linger_s`
  after the send so the firmware's ~4 s broadcast politeness delay and the
  packet's airtime complete before the close-triggered DTR reset drops the queued
  TX. It is paid per burst per step, so it dominates sweep wall-clock. `pa_sweep`
  leaves `tx_linger_s=None` by default and **auto-derives** it from the node's
  live preset time-on-air (Semtech AN1200.13, `lora_time_on_air_s`): politeness +
  airtime + margin. A fast preset (LONG_FAST ~200 B ≈ 2 s airtime) gets ~7 s;
  LONG_SLOW (~12 s airtime) gets ~18 s — no clipping and no manual tuning. Pass a
  number to override; the value used is echoed as `tx_linger_s` in the result.

Every reading is an uncalibrated bench regression check (±0.5 dB instrument plus a
hand-entered attenuator value), not a substitute for certified EMC-lab compliance
measurement.

## Tools

| Tool | Kind | Purpose |
|------|------|---------|
| `pa_meter_status` | read-only | Detect the meter; report version, stored frequency, and a live avg/peak reading. Returns `{"present": false, ...}` when none is attached. |
| `pa_measure`      | write (meter only) | Reads min/mean/max dBm over N samples; **selects the band's active calibration curve** on the meter (transient, not persisted), so it is not read-only. Harmless to any device under test — no Meshtastic device driven. |
| `pa_sweep`        | destructive (`confirm=True`) | Closed-loop: step `lora.tx_power`, key TX, measure the PA output at each step; returns a configured-vs-measured table plus a compression/saturation analysis. |

The tools are always registered (they cost nothing without a meter) and return a
clear "no meter" result or error when one is not attached — deliberately, so
`pa_meter_status` is available precisely when you suspect the meter has powered
itself off. `doctor` reports meter presence under the `power_meter` group.

## Reference implementations

- [ExpressLRS/RfPowerMeter](https://github.com/ExpressLRS/RfPowerMeter) — the
  official ELRS Python CLI (pyserial, VID/PID auto-detect, `F<idx>` twice, CSV
  logging).
- [SunjunKim/rf_power_meter_logger](https://github.com/SunjunKim/rf_power_meter_logger)
  — a Processing GUI logger whose header documents the V/D/E/F command set.
