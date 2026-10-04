# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Python port of firmware's region/preset → RF-parameter resolution.

This is the "ground truth" half of the SDR compliance oracle (`sdr.py` is the
measurement half): given the *configured* LoRa settings a device reports over
admin (region, modem preset, channel name/number, frequency override/offset),
predict the exact center frequency, bandwidth, spreading factor, coding rate,
and the regulatory duty-cycle/power limits firmware itself would compute and
apply. `rf_confirm_tx` (server.py) compares this prediction against what an
SDR actually observes on air — an independent check that the radio is doing
what its own config says it should, not just what it self-reports.

Sources (Meshtastic 3.0):

  - Regions, region profiles, modem presets and the `LoRaConfig.bandwidth`
    code table: the protobufs registry (`registry/generated/regions.json`,
    `modem_presets.json`), bundled by the meshtastic package next to its
    generated protobufs. Firmware generates its own tables from the same
    files (`bin/gen-regions.py`), so there is nothing here to re-sync.
  - Slot plan: protobufs SCHEMA.md section 6, which firmware computes in
    `src/mesh/SlotPlan.cpp` — half-hertz integers, sub-bands, channel
    rasters, edge clearance.
  - Channel name hash (djb2) and slot selection:
    `src/mesh/RadioInterface.cpp` — `hash()` and `applyModemConfig()`.
  - Role-dependent duty cycle (EU_866/EU_874/EU_917 routers):
    `src/mesh/regulatory/RegionHooks.cpp`.
  - Modem preset display names (the default channel name, hashed for slot
    selection): `ModemPresetInfo.name` in the registry.

Deliberately NOT ported: PSK-based channel hash (`Channels::generateHash`,
used only for the on-air channel-routing byte, not frequency selection), the
full `checkOrClampConfigLora` validation/clamping path and the JP airtime
hook — we predict for already-valid configs as reported by a live device's
admin/config readback, we don't need to re-validate them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import meshtastic

_REGISTRY_DIR = os.path.join(os.path.dirname(os.path.abspath(meshtastic.__file__)), "protobuf")


def _load(name: str) -> dict:
    with open(os.path.join(_REGISTRY_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _bare(name: str, prefix: str) -> str:
    return name[len(prefix) :] if name.startswith(prefix) else name


# ---------------------------------------------------------------------------
# Modem presets and bandwidth codes — registry modem_presets.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetParams:
    bw_hz: int  # nominal, the slot plan's input
    bw_hz_wide: int  # in a `wide_lora` region (2.4GHz); 0 = no wide form
    sf: int
    cr: int
    name: str  # display name: the default channel name, hashed for slot selection


_PRESET_DOC = _load("modem_presets.json")

# Keys are the ModemPreset enum names without the MODEM_ prefix.
PRESETS: dict[str, PresetParams] = {
    _bare(p["preset"], "MODEM_"): PresetParams(
        p["bandwidth_hz"], p["wide_bandwidth_hz"], p["spread_factor"], p["coding_rate"], p["name"]
    )
    for p in _PRESET_DOC["presets"]
}

# LoRaConfig.bandwidth wire code -> nominal Hz; a code not listed is kHz as it stands.
BANDWIDTH_CODES: dict[int, int] = {
    c["code"]: c["bandwidth_hz"] for c in _PRESET_DOC["bandwidth_codes"]
}

# ---------------------------------------------------------------------------
# Regions and profiles — registry regions.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionProfile:
    spacing_hz: int
    padding_hz: int  # derived from the raster when unit_channel_hz is set
    unit_channel_hz: int  # regulatory channel raster, 0 = continuous spectrum
    max_bandwidth_hz: int  # 0 = no cap


@dataclass(frozen=True)
class RegionInfo:
    freq_start_hz: int
    freq_end_hz: int
    sub_bands_hz: tuple[tuple[int, int], ...]  # empty = one block equal to the band edges
    duty_cycle_pct: float  # 100 == no regulatory duty-cycle limit (still LBT-gated by firmware)
    power_limit_dbm: int
    freq_switching: bool
    wide_lora: bool
    edge_clearance: bool
    profile: RegionProfile
    default_preset: str
    # 0 = channel-name hash (default), -1 = preset-name hash, >0 = explicit 1-based slot
    override_slot: int

    @property
    def freq_start_mhz(self) -> float:
        return self.freq_start_hz / 1e6

    @property
    def freq_end_mhz(self) -> float:
        return self.freq_end_hz / 1e6


def _profile(p: dict) -> RegionProfile:
    return RegionProfile(
        p["spacing_hz"], p.get("padding_hz", 0), p["unit_channel_hz"], p["max_bandwidth_hz"]
    )


_REGION_DOC = _load("regions.json")
_PROFILES = {p["name"]: p for p in _REGION_DOC["profiles"]}

# Keys are the RegionCode enum names without the REGION_ prefix. UNSET is a copy
# of US that firmware keeps silent.
REGIONS: dict[str, RegionInfo] = {
    _bare(r["region_code"], "REGION_"): RegionInfo(
        freq_start_hz=r["freq_start_hz"],
        freq_end_hz=r["freq_end_hz"],
        sub_bands_hz=tuple((b["start_hz"], b["end_hz"]) for b in r.get("sub_bands", [])),
        duty_cycle_pct=r["duty_cycle_permille"] / 10.0,
        power_limit_dbm=r["power_limit_dbm"],
        freq_switching=r["frequency_switching"],
        wide_lora=r["wide_lora"],
        edge_clearance=r["edge_clearance"],
        profile=_profile(_PROFILES[r["profile"]]),
        default_preset=_bare(_PROFILES[r["profile"]]["default_preset"], "MODEM_"),
        override_slot=r["override_slot"],
    )
    for r in _REGION_DOC["regions"]
}

_OVERRIDE_SLOT_CHANNEL_HASH = 0
_OVERRIDE_SLOT_PRESET_HASH = -1

# Edge clearance never leaves fewer gaps than this.
_CLEARANCE_MIN_GAPS = 4

# 2006/771/EC band 47b and 2022/172 bands 1 and 4: 10 % for network access points.
_EU_DATA_NETWORK_REGIONS = frozenset({"EU_866", "EU_874", "EU_917"})


def _segment(start: int, end: int, bw: int, profile: RegionProfile, edge_clearance: bool):
    """One block's (count, pitch, first_centre) in half-hertz, or None when no slot fits."""
    span, b = 2 * (end - start), 2 * bw
    spacing, unit = 2 * profile.spacing_hz, 2 * profile.unit_channel_hz
    padding = (-(-b // unit) * unit - b) // 2 if unit else 2 * profile.padding_hz
    if span < 2 * padding + b:
        return None
    pitch = spacing + 2 * padding + b
    count = (span + spacing) // pitch

    def extent(n: int) -> int:
        return n * (b + 2 * padding) + (n - 1) * spacing

    if (
        edge_clearance
        and 2 * (span - extent(count)) + 4 * padding < b
        and count - 1 >= _CLEARANCE_MIN_GAPS
    ):
        count -= 1
    offset = (span - extent(count)) // 2
    if unit:
        offset = offset // unit * unit
    return count, pitch, 2 * start + offset + padding + b // 2


def slot_plan(region: RegionInfo, bandwidth_hz: int) -> list[tuple[int, int, int]]:
    """(count, pitch, first_centre) in half-hertz for each block holding a slot,
    ascending. Empty means the region has no slot for this bandwidth. Port of
    protobufs SCHEMA.md section 6 (`computeSlotPlan` in firmware)."""
    cap = region.profile.max_bandwidth_hz
    if bandwidth_hz <= 0 or (cap and bandwidth_hz > cap):
        return []
    blocks = region.sub_bands_hz or ((region.freq_start_hz, region.freq_end_hz),)
    segments = (
        _segment(a, b, bandwidth_hz, region.profile, region.edge_clearance) for a, b in blocks
    )
    return [s for s in segments if s]


def slot_centre_hz(plan: list[tuple[int, int, int]], slot: int) -> float:
    """Centre of a 0-based slot, numbered across blocks in ascending frequency."""
    for count, pitch, first in plan:
        if slot < count:
            return (first + slot * pitch) / 2
        slot -= count
    raise IndexError(slot)


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
    bandwidth: int | None = None,
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
    straight through. `bandwidth` is the `LoRaConfig.bandwidth` wire code
    (62 is 62.5 kHz). `channel_name` is the *primary* channel's name (empty
    string if unset — matches firmware's "no custom name" case and falls back
    to the preset display name for hashing, exactly like real devices).

    Raises ValueError for an unknown region/preset, or a bandwidth the region
    has no slot for (rather than silently guessing).
    """
    if region not in REGIONS:
        raise ValueError(
            f"Unknown Meshtastic region {region!r} (not in the bundled region registry)"
        )
    r = REGIONS[region]

    if use_preset:
        if modem_preset not in PRESETS:
            raise ValueError(
                f"Unknown modem preset {modem_preset!r} (not in the bundled preset registry)"
            )
        p = PRESETS[modem_preset]
        bw_hz = p.bw_hz_wide if (r.wide_lora and p.bw_hz_wide) else p.bw_hz
        sf = p.sf
        cr = p.cr
        preset_name = p.name
    else:
        if bandwidth is None or spread_factor is None or coding_rate is None:
            raise ValueError("use_preset=False requires bandwidth/spread_factor/coding_rate")
        bw_hz = BANDWIDTH_CODES.get(bandwidth, bandwidth * 1000)
        sf, cr = spread_factor, coding_rate
        preset_name = (
            "Custom"  # DisplayFormatters::getModemPresetDisplayName() with use_preset clear
        )

    duty_cycle_pct = r.duty_cycle_pct
    if region in _EU_DATA_NETWORK_REGIONS and device_role in ("ROUTER", "ROUTER_LATE"):
        duty_cycle_pct = 10.0

    # override_frequency wins outright — channel_num is meaningless in that mode.
    if override_frequency_mhz:
        return PredictedRf(
            region=region,
            freq_mhz=override_frequency_mhz + frequency_offset_mhz,
            bw_khz=bw_hz / 1000.0,
            sf=sf,
            cr=cr,
            channel_num=-1,
            num_freq_slots=0,
            duty_cycle_pct=duty_cycle_pct,
            power_limit_dbm=r.power_limit_dbm,
            wide_lora=r.wide_lora,
        )

    plan = slot_plan(r, bw_hz)
    num_slots = sum(count for count, _, _ in plan)
    if num_slots <= 0:
        raise ValueError(f"{region}: no slot for {bw_hz / 1000.0}kHz bandwidth")

    effective_channel_name = channel_name or preset_name
    channel_name_hash_slot = djb2_hash(effective_channel_name) % num_slots
    preset_name_hash_slot = djb2_hash(preset_name) % num_slots

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
    # A region's fixed slot can lie beyond a custom bandwidth's plan; firmware wraps it
    resolved_slot %= num_slots

    return PredictedRf(
        region=region,
        freq_mhz=slot_centre_hz(plan, resolved_slot) / 1e6 + frequency_offset_mhz,
        bw_khz=bw_hz / 1000.0,
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
