# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Faithful Python port of firmware's region/preset → RF-parameter resolution.

This is the "ground truth" half of the SDR compliance oracle (`sdr.py` is the
measurement half): given the *configured* LoRa settings a device reports over
admin (region, modem preset, channel name/number, frequency override/offset),
predict the exact center frequency, bandwidth, spreading factor, coding rate,
and the regulatory duty-cycle/power limits firmware itself would compute and
apply. `rf_confirm_tx` (server.py) compares this prediction against what an
SDR actually observes on air — an independent check that the radio is doing
what its own config says it should, not just what it self-reports.

Every table and formula here is transcribed from the firmware source it
mirrors, with file:line citations, so it can be re-synced when upstream
changes. Source: `meshtastic/firmware` (checkout used: $MESHTASTIC_FIRMWARE_ROOT
when set, else verify manually before trusting for new regions/presets):

  - Region table (freq band, duty cycle, power limit, profile, override slot):
    `src/mesh/RadioInterface.cpp` — `const RegionInfo regions[]`
  - Region profile table (channel spacing/padding):
    `src/mesh/RadioInterface.cpp` — `PROFILE_STD` / `PROFILE_EU868` / ...
  - Modem preset → bandwidth/SF/CR:
    `src/mesh/MeshRadio.h` — `modemPresetToParams()`
  - Channel name hash (djb2) and frequency-slot formula:
    `src/mesh/RadioInterface.cpp` — `hash()` and `applyModemConfig()`
  - Per-role duty cycle override (EU_866 router vs. mobile):
    `src/mesh/RadioInterface.cpp` — `getEffectiveDutyCycle()`
  - Modem preset display names (used as the default channel name when the
    primary channel has no custom name — this is what gets hashed):
    `src/DisplayFormatters.cpp` — `getModemPresetDisplayName()`

Deliberately NOT ported: PSK-based channel hash (`Channels::generateHash`,
used only for the on-air channel-routing byte, not frequency selection) and
the full `checkOrClampConfigLora` validation/clamping path — we predict for
already-valid configs as reported by a live device's admin/config readback,
we don't need to re-validate them.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Modem presets — src/mesh/MeshRadio.h: modemPresetToParams()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetParams:
    bw_khz: float
    bw_khz_wide: float  # `wideLora` regions (2.4GHz, SX128x) use a scaled bandwidth instead
    sf: int
    cr: int


# Keys are the modem_preset enum names as used in the Config protobuf
# (`meshtastic.Config.LoRaConfig.ModemPreset`).
PRESETS: dict[str, PresetParams] = {
    "SHORT_TURBO": PresetParams(500.0, 1625.0, sf=7, cr=5),
    "SHORT_FAST": PresetParams(250.0, 812.5, sf=7, cr=5),
    "SHORT_SLOW": PresetParams(250.0, 812.5, sf=8, cr=5),
    "MEDIUM_FAST": PresetParams(250.0, 812.5, sf=9, cr=5),
    "MEDIUM_SLOW": PresetParams(250.0, 812.5, sf=10, cr=5),
    "LONG_TURBO": PresetParams(500.0, 1625.0, sf=11, cr=8),
    "LONG_MODERATE": PresetParams(125.0, 406.25, sf=11, cr=8),
    "LONG_FAST": PresetParams(250.0, 812.5, sf=11, cr=5),
    "LONG_SLOW": PresetParams(125.0, 406.25, sf=12, cr=8),
    "LITE_FAST": PresetParams(125.0, 125.0, sf=9, cr=5),
    "LITE_SLOW": PresetParams(125.0, 125.0, sf=10, cr=5),
    "NARROW_FAST": PresetParams(62.5, 62.5, sf=7, cr=6),
    "NARROW_SLOW": PresetParams(62.5, 62.5, sf=8, cr=6),
    "TINY_FAST": PresetParams(15.6, 15.6, sf=7, cr=5),
    "TINY_SLOW": PresetParams(15.6, 15.6, sf=8, cr=6),
}

# DisplayFormatters::getModemPresetDisplayName(preset, useShortName=false, usePreset=true)
# This is the string that becomes the *default* channel name (and therefore
# what gets hashed for frequency-slot selection) when the primary channel has
# no custom name set — i.e. almost every out-of-the-box device.
PRESET_DISPLAY_NAME: dict[str, str] = {
    "SHORT_TURBO": "ShortTurbo",
    "SHORT_SLOW": "ShortSlow",
    "SHORT_FAST": "ShortFast",
    "MEDIUM_SLOW": "MediumSlow",
    "MEDIUM_FAST": "MediumFast",
    "LONG_SLOW": "LongSlow",
    "LONG_FAST": "LongFast",
    "LONG_TURBO": "LongTurbo",
    "LONG_MODERATE": "LongMod",
    "LITE_FAST": "LiteFast",
    "LITE_SLOW": "LiteSlow",
    "NARROW_FAST": "NarrowFast",
    "NARROW_SLOW": "NarrowSlow",
    "TINY_FAST": "TinyFast",
    "TINY_SLOW": "TinySlow",
}

# ---------------------------------------------------------------------------
# Region profiles — src/mesh/RadioInterface.cpp: PROFILE_* (spacing, padding)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionProfile:
    spacing_mhz: float
    padding_mhz: float


PROFILE_STD = RegionProfile(0.0, 0.0)
PROFILE_EU868 = RegionProfile(0.0, 0.0)
PROFILE_UNDEF = RegionProfile(0.0, 0.0)
PROFILE_LITE = RegionProfile(0.4, 0.0375)
PROFILE_NARROW = RegionProfile(0.0, 0.0104)
PROFILE_HAM_20KHZ = RegionProfile(0.0, 0.0022)
PROFILE_HAM_100KHZ = RegionProfile(0.0, 0.01875)

# ---------------------------------------------------------------------------
# Region table — src/mesh/RadioInterface.cpp: const RegionInfo regions[]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionInfo:
    freq_start_mhz: float
    freq_end_mhz: float
    duty_cycle_pct: float  # 100 == no regulatory duty-cycle limit (still LBT-gated by firmware)
    power_limit_dbm: int
    freq_switching: bool
    wide_lora: bool
    profile: RegionProfile
    default_preset: str
    # 0 = channel-name hash (default), -1 = preset-name hash, >0 = explicit 1-based slot
    override_slot: int


REGIONS: dict[str, RegionInfo] = {
    "US": RegionInfo(902.0, 928.0, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "EU_433": RegionInfo(433.0, 434.0, 10, 10, False, False, PROFILE_STD, "LONG_FAST", 0),
    "EU_868": RegionInfo(869.4, 869.65, 10, 27, False, False, PROFILE_EU868, "LONG_FAST", 0),
    "EU_866": RegionInfo(865.6, 867.6, 2.5, 27, False, False, PROFILE_LITE, "LITE_FAST", 0),
    "EU_N_868": RegionInfo(869.4, 869.65, 10, 27, False, False, PROFILE_NARROW, "NARROW_SLOW", 1),
    "CN": RegionInfo(470.0, 510.0, 100, 19, False, False, PROFILE_STD, "LONG_FAST", 0),
    "JP": RegionInfo(920.5, 923.5, 100, 13, False, False, PROFILE_STD, "LONG_FAST", 0),
    "ANZ": RegionInfo(915.0, 928.0, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "ANZ_433": RegionInfo(433.05, 434.79, 100, 14, False, False, PROFILE_STD, "LONG_FAST", 0),
    "RU": RegionInfo(868.7, 869.2, 100, 20, False, False, PROFILE_STD, "LONG_FAST", 0),
    "KR": RegionInfo(920.0, 923.0, 100, 23, False, False, PROFILE_STD, "LONG_FAST", 0),
    "TW": RegionInfo(920.0, 925.0, 100, 27, False, False, PROFILE_STD, "LONG_FAST", 0),
    "IN": RegionInfo(865.0, 867.0, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "NZ_865": RegionInfo(864.0, 868.0, 100, 36, False, False, PROFILE_STD, "LONG_FAST", 0),
    "TH": RegionInfo(920.0, 925.0, 10, 27, False, False, PROFILE_STD, "LONG_FAST", 0),
    "UA_433": RegionInfo(433.0, 434.7, 10, 10, False, False, PROFILE_STD, "LONG_FAST", 0),
    "UA_868": RegionInfo(868.0, 868.6, 1, 14, False, False, PROFILE_STD, "LONG_FAST", 0),
    "MY_433": RegionInfo(433.0, 435.0, 100, 20, False, False, PROFILE_STD, "LONG_FAST", 0),
    "MY_919": RegionInfo(919.0, 924.0, 100, 27, True, False, PROFILE_STD, "LONG_FAST", 0),
    "SG_923": RegionInfo(917.0, 925.0, 100, 20, False, False, PROFILE_STD, "LONG_FAST", 0),
    "PH_433": RegionInfo(433.0, 434.7, 100, 10, False, False, PROFILE_STD, "LONG_FAST", 0),
    "PH_868": RegionInfo(868.0, 869.4, 100, 14, False, False, PROFILE_STD, "LONG_FAST", 0),
    "PH_915": RegionInfo(915.0, 918.0, 100, 24, False, False, PROFILE_STD, "LONG_FAST", 0),
    "KZ_433": RegionInfo(433.075, 434.775, 100, 10, False, False, PROFILE_STD, "LONG_FAST", 0),
    "KZ_863": RegionInfo(863.0, 868.0, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "NP_865": RegionInfo(865.0, 868.0, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "BR_902": RegionInfo(902.0, 907.5, 100, 30, False, False, PROFILE_STD, "LONG_FAST", 0),
    "ITU1_2M": RegionInfo(144.0, 146.0, 100, 30, False, False, PROFILE_HAM_20KHZ, "TINY_FAST", 26),
    "ITU2_2M": RegionInfo(144.0, 148.0, 100, 30, False, False, PROFILE_HAM_20KHZ, "TINY_FAST", 51),
    "ITU3_2M": RegionInfo(144.0, 148.0, 100, 30, False, False, PROFILE_HAM_20KHZ, "TINY_FAST", 33),
    "ITU2_125CM": RegionInfo(
        220.0, 225.0, 100, 30, False, False, PROFILE_HAM_100KHZ, "NARROW_SLOW", 37
    ),
    "LORA_24": RegionInfo(2400.0, 2483.5, 100, 10, False, True, PROFILE_STD, "LONG_FAST", 0),
    # "This needs to be last. Same as US." (also the array sentinel in firmware) — kept here for
    # completeness/lookup only; not meaningfully different from a real default-region prediction.
    "UNSET": RegionInfo(902.0, 928.0, 100, 30, False, False, PROFILE_UNDEF, "LONG_FAST", 0),
}

_OVERRIDE_SLOT_CHANNEL_HASH = 0
_OVERRIDE_SLOT_PRESET_HASH = -1


def djb2_hash(s: str) -> int:
    """Port of `src/mesh/RadioInterface.cpp: uint32_t hash(const char *str)`.

    Bernstein djb2, computed with native firmware semantics: `uint32_t` 32-bit
    wraparound on overflow (C's defined unsigned-int behavior).
    """
    h = 5381
    for ch in s:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return h


@dataclass(frozen=True)
class PredictedRf:
    """What firmware's `applyModemConfig()` would compute for these settings."""

    region: str
    freq_mhz: float
    bw_khz: float
    sf: int
    cr: int
    channel_num: int  # 0-based "frequency slot", as actually used on air
    num_freq_slots: int
    duty_cycle_pct: float
    power_limit_dbm: int
    wide_lora: bool


def predict_lora_params(
    region: str,
    modem_preset: str,
    *,
    channel_name: str = "",
    channel_num: int = 0,
    use_preset: bool = True,
    bandwidth_khz: float | None = None,
    spread_factor: int | None = None,
    coding_rate: int | None = None,
    override_frequency_mhz: float = 0.0,
    frequency_offset_mhz: float = 0.0,
    device_role: str = "CLIENT",
) -> PredictedRf:
    """Predict the on-air center frequency/BW/SF/CR/duty-cycle for a device's
    *configured* LoRa settings, mirroring `RadioInterface::applyModemConfig()`.

    Args mirror the admin-readable `Config.LoRaConfig` fields directly: read
    them off a live device (`device_info()` / `get_config()`) and pass them
    straight through. `channel_name` is the *primary* channel's name (empty
    string if unset — matches firmware's "no custom name" case and falls back
    to the preset display name for hashing, exactly like real devices).

    Raises ValueError for an unknown region/preset (rather than silently
    guessing) — the region/preset tables above should be re-synced from
    firmware if this fires for a name that's legitimately new upstream.
    """
    if region not in REGIONS:
        raise ValueError(
            f"Unknown Meshtastic region {region!r} "
            "(region table may be stale — see module docstring)"
        )
    r = REGIONS[region]

    if use_preset:
        if modem_preset not in PRESETS:
            raise ValueError(
                f"Unknown modem preset {modem_preset!r} (table may be stale, see module docstring)"
            )
        p = PRESETS[modem_preset]
        bw_khz = p.bw_khz_wide if r.wide_lora else p.bw_khz
        sf = p.sf
        cr = p.cr
    else:
        if bandwidth_khz is None or spread_factor is None or coding_rate is None:
            raise ValueError("use_preset=False requires bandwidth_khz/spread_factor/coding_rate")
        bw_khz, sf, cr = bandwidth_khz, spread_factor, coding_rate

    # Effective duty cycle: EU_866 is role-dependent (getEffectiveDutyCycle()), all other
    # regions just use the table value.
    duty_cycle_pct = r.duty_cycle_pct
    if region == "EU_866":
        duty_cycle_pct = 10.0 if device_role in ("ROUTER", "ROUTER_LATE") else 2.5

    # override_frequency wins outright — channel_num is meaningless in that mode.
    if override_frequency_mhz:
        freq = override_frequency_mhz + frequency_offset_mhz
        return PredictedRf(
            region=region,
            freq_mhz=freq,
            bw_khz=bw_khz,
            sf=sf,
            cr=cr,
            channel_num=-1,
            num_freq_slots=0,
            duty_cycle_pct=duty_cycle_pct,
            power_limit_dbm=r.power_limit_dbm,
            wide_lora=r.wide_lora,
        )

    freq_slot_width_mhz = r.profile.spacing_mhz + (r.profile.padding_mhz * 2) + (bw_khz / 1000.0)
    num_slots = round(
        (r.freq_end_mhz - r.freq_start_mhz + r.profile.spacing_mhz) / freq_slot_width_mhz
    )
    if num_slots <= 0:
        raise ValueError(f"{region}/{modem_preset}: {bw_khz}kHz bandwidth doesn't fit in the band")

    effective_channel_name = channel_name or PRESET_DISPLAY_NAME.get(modem_preset, "Custom")
    channel_name_hash_slot = djb2_hash(effective_channel_name) % num_slots
    preset_name_hash_slot = djb2_hash(PRESET_DISPLAY_NAME.get(modem_preset, "Custom")) % num_slots

    # channel_num == 0 is firmware's "unset, use the default slot" sentinel (1-based on the
    # wire; applyModemConfig() treats this the same as "uses_default_frequency_slot").
    if channel_num == 0:
        if r.override_slot > 0:
            resolved_slot = r.override_slot - 1
        elif r.override_slot == _OVERRIDE_SLOT_PRESET_HASH:
            resolved_slot = preset_name_hash_slot
        else:
            resolved_slot = channel_name_hash_slot
    else:
        resolved_slot = channel_num - 1

    freq = r.freq_start_mhz + (bw_khz / 2000.0) + r.profile.padding_mhz
    freq += resolved_slot * freq_slot_width_mhz
    freq += frequency_offset_mhz

    return PredictedRf(
        region=region,
        freq_mhz=freq,
        bw_khz=bw_khz,
        sf=sf,
        cr=cr,
        channel_num=resolved_slot,
        num_freq_slots=num_slots,
        duty_cycle_pct=duty_cycle_pct,
        power_limit_dbm=r.power_limit_dbm,
        wide_lora=r.wide_lora,
    )


def effective_power_limit_dbm(
    region: str, configured_tx_power_dbm: int, *, is_licensed: bool = False
) -> int:
    """Port of the tx power clamp in `applyModemConfig()`:

    if ((power == 0) || ((power > newRegion->powerLimit) && !is_licensed))
        power = newRegion->powerLimit;
    if (power == 0)
        power = 17;
    """
    r = REGIONS[region]
    power = configured_tx_power_dbm
    if power == 0 or (power > r.power_limit_dbm and not is_licensed):
        power = r.power_limit_dbm
    if power == 0:
        power = 17
    return power
