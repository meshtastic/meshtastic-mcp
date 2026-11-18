# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Result shapes tools advertise as their MCP `outputSchema`.

Clients validate a tool's result against this schema, so a field's type must hold for every value
the tool can return. A `total=False` shape leaves every key optional; the others require the keys
every return path carries and mark the rest `NotRequired`. Unlisted keys are allowed, and a field
whose type varies stays `Any`. Claude Code does not show the model an outputSchema, so each tool's
description also names its result fields.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any, Literal, NotRequired

from pydantic import Field

# pydantic builds the output schemas from these, and before Python 3.12 it accepts only the
# typing_extensions TypedDict.
if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict


class DeviceEntry(TypedDict, total=False):
    port: str
    vid: str | None
    pid: str | None
    description: str | None
    manufacturer: str | None
    product: str | None
    serial_number: str | None
    likely_meshtastic: bool
    blacklisted: bool


class DeviceInfo(TypedDict, total=False):
    port: str | None
    my_node_num: int | None
    long_name: str | None
    short_name: str | None
    firmware_version: str | None
    hw_model: Any
    region: str | None
    num_nodes: int
    primary_channel: str | None
    modem_preset: str | None
    channel_num: int | None
    display: Any


class NodeUser(TypedDict, total=False):
    long_name: str | None
    short_name: str | None
    hw_model: Any
    role: Any


class NodePosition(TypedDict, total=False):
    latitude: float | None
    longitude: float | None
    altitude: float | None
    time: int | None


class NodeEntry(TypedDict, total=False):
    node_num: int | None
    user: NodeUser
    position: NodePosition | None
    snr: float | None
    rssi: float | None
    last_heard: int | None
    battery_level: float | None
    is_favorite: bool


class QueryWindow(TypedDict, total=False):
    start: Any
    end: Any


class LogsWindow(TypedDict, total=False):
    lines: list[dict[str, Any]]
    total_matched: int
    dropped: int
    window: QueryWindow


class PacketsWindow(TypedDict, total=False):
    packets: list[dict[str, Any]]
    total_matched: int
    dropped: int
    window: QueryWindow


class EventsWindow(TypedDict, total=False):
    events: list[dict[str, Any]]
    total_matched: int
    dropped: int
    window: QueryWindow


class DoctorReport(TypedDict, total=False):
    platform: Any
    capabilities: Any
    ok: bool
    checks: list[dict[str, Any]]
    missing: list[str]
    degraded: list[str]
    fix_commands: list[str]


# ---------- device writes ---------------------------------------------------


class SetOwnerResult(TypedDict):
    """`set_owner` result."""

    ok: bool
    long_name: str
    short_name: Annotated[str | None, Field(description="null when the call passed none")]


class SetConfigResult(TypedDict):
    """`set_config` result."""

    ok: bool
    path: str
    section: str
    parent: Annotated[str, Field(description="`localConfig` or `moduleConfig`")]
    old_value: Annotated[Any, Field(description="enum fields by name")]
    new_value: Annotated[Any, Field(description="enum fields by name")]


class SetChannelUrlResult(TypedDict):
    """`set_channel_url` result."""

    ok: bool
    channels_imported: Annotated[int, Field(description="channels not DISABLED after the import")]


class SendTextResult(TypedDict):
    """`send_text` result; the `tx_*` keys come only with `wait_for_tx=True`."""

    ok: bool
    packet_id: int | None
    destination: Annotated[str | int, Field(description="`^all` for a broadcast")]
    tx_confirmed: NotRequired[
        Annotated[
            bool | None,
            Field(
                description=(
                    "true: TX seen; false: logs flowed and showed no TX; null: not observable, "
                    "usually because firmware logs are not captured, which is not a failure"
                )
            ),
        ]
    ]
    tx_latency_s: NotRequired[float | None]
    tx_unconfirmed_reason: NotRequired[
        Annotated[str, Field(description="why `tx_confirmed` is false or null")]
    ]


class RebootResult(TypedDict):
    """`reboot` result."""

    ok: bool
    rebooting_in_s: int


class ShutdownResult(TypedDict):
    """`shutdown` result."""

    ok: bool
    shutting_down_in_s: int


class FactoryResetResult(TypedDict):
    """`factory_reset` result."""

    ok: bool
    full: Annotated[bool, Field(description="identity and keys wiped too")]


class SendInputEventResult(TypedDict):
    """`send_input_event` result."""

    ok: bool
    event_code: Annotated[int, Field(description="the numeric code a name resolved to")]
    kb_char: int


# ---------- config snapshots ------------------------------------------------


class ConfigSnapshotResult(TypedDict):
    """`config_snapshot` result."""

    ok: bool
    name: str
    path: str
    local_sections: list[str]
    module_sections: list[str]


ConfigChange = TypedDict("ConfigChange", {"from": Any, "to": Any})

ConfigDiffResult = TypedDict(
    "ConfigDiffResult",
    {
        "from": Annotated[str, Field(description="`name_a`")],
        "to": Annotated[str, Field(description="`name_b`, or `live` for the device")],
        "changed": Annotated[
            dict[str, ConfigChange], Field(description="dot-path to `{from, to}`")
        ],
        "added": Annotated[dict[str, Any], Field(description="dot-path to value")],
        "removed": Annotated[dict[str, Any], Field(description="dot-path to value")],
        "identical": bool,
    },
)
ConfigDiffResult.__doc__ = "`config_diff` result, keyed by dot-path (`localConfig.lora.region`)."


# ---------- screen, flash, serial -------------------------------------------


class CaptureDisplayResult(TypedDict):
    """`capture_display` result."""

    path: str
    bytes: Annotated[int, Field(description="PNG size")]
    width: Annotated[int, Field(description="PNG width, after `scale`")]
    height: Annotated[int, Field(description="PNG height, after `scale`")]
    panel_width: int
    panel_height: int
    scale: int
    format: Literal["RGB565", "MONO_VLSB"]
    frames: int
    rects: int
    colorized: bool
    blank: Annotated[
        bool, Field(description="a single color; almost always the panel had timed out")
    ]
    screen_on_secs: Annotated[int | None, Field(description="the panel's blank timeout")]
    panel_class: Annotated[str | None, Field(description="from the handshake, e.g. `EINK`")]


class FlashStartResult(TypedDict):
    """`flash_start` result; poll `flash_poll` with `job_id`."""

    job_id: str
    status: Literal["running"]
    log_path: str


class SerialCloseResult(TypedDict):
    """`serial_close` result."""

    ok: Annotated[bool, Field(description="false when `session_id` is unknown")]
    reason: NotRequired[str]


# ---------- PA bench (ImmersionRC RF Power Meter v2) ------------------------


class PaMeterStatus(TypedDict):
    """`pa_meter_status` result: a reading, `detail` when no meter is attached, or `error`."""

    present: bool
    detail: NotRequired[str]
    error: NotRequired[Annotated[str, Field(description="the attached meter did not answer")]]
    port: NotRequired[str]
    version: NotRequired[str]
    stored_freq_mhz: NotRequired[int]
    current_avg_dbm: NotRequired[float]
    current_peak_dbm: NotRequired[float]


class PaMeasureResult(TypedDict):
    """`pa_measure` result, corrected for `attenuator_db`."""

    band: str
    requested_center_mhz: float
    meter_cal_mhz: Annotated[int, Field(description="the calibration point the band snapped to")]
    kind: Literal["peak", "average"]
    attenuator_db: float
    samples: int
    min_dbm: float
    mean_dbm: float
    max_dbm: float


class PaSweepStep(TypedDict):
    """One configured power; the measured fields are null for a silent step."""

    configured_dbm: int
    measured_avg_dbm: float | None
    measured_peak_dbm: float | None
    delta_db: float | None
    active_samples: int
    total_samples: int
    rf_observed: bool


class PaCurve(TypedDict):
    """The gain-curve analysis over the measured steps."""

    points: int
    saturation_dbm: Annotated[
        int | None, Field(description="first step where gain per configured dB falls below 0.5")
    ]
    max_measured_dbm: float | None
    max_measured_at_configured_dbm: NotRequired[int]
    offset_at_min_db: float | None
    monotonic: bool
    note: NotRequired[Annotated[str, Field(description="set when under 2 steps were measured")]]


class PaSweepResult(TypedDict):
    """`pa_sweep` result."""

    band: str
    region: str | int
    requested_center_mhz: float
    meter_cal_mhz: int
    attenuator_db: float
    tx_linger_s: Annotated[float, Field(description="the per-burst linger used")]
    floor_dbm: float
    floor_margin_db: float
    table: list[PaSweepStep]
    curve: PaCurve
    silent_steps_dbm: Annotated[
        list[int], Field(description="a dead PA or a late TX; rerun spaced out")
    ]
    config_restored: bool
    restore_errors: list[str]
    caveat: str


# ---------- BLE sniffer -----------------------------------------------------


class BleSnifferDongle(TypedDict):
    port: str
    vid: int
    pid: int
    serial_number: str | None


class BleActiveCapture(TypedDict):
    job_id: str
    port: str | None
    pcap_path: str | None


class BleSniffStatus(TypedDict):
    """`ble_sniff_status` result."""

    ready: bool
    sniffers: list[BleSnifferDongle]
    sniffer_bin: str | None
    missing: list[str]
    hint: Annotated[str | None, Field(description="how to get what is missing")]
    capture_dir: str
    transmit_supported: Annotated[bool, Field(description="always false")]
    active_captures: list[BleActiveCapture]
