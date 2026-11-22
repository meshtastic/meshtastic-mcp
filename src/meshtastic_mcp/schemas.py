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


class InspectResult(TypedDict):
    target: Any
    backend: str
    summary: NotRequired[dict[str, Any]]
    config: NotRequired[dict[str, Any]]
    channels: NotRequired[list[dict[str, Any]]]
    url: NotRequired[dict[str, str]]
    contact_url: NotRequired[str]
    nodes: NotRequired[list[dict[str, Any]]]
    network: NotRequired[dict[str, Any]]
    errors: NotRequired[dict[str, str]]


class AdminResult(TypedDict):
    target: Any
    action: str
    ok: bool
    dest: NotRequired[str]
    node_id: NotRequired[Any]
    time: NotRequired[int]
    back: NotRequired[bool | None]
    restarted: NotRequired[bool]
    uptime_s: NotRequired[float]
    error: NotRequired[str]


class ConfigureResult(TypedDict):
    target: Any
    dry_run: bool
    changes: dict[str, Any]
    verified: bool | None
    restarted: bool | None
    snapshot: NotRequired[dict[str, str]]
    transaction: NotRequired[bool]
    mismatches: NotRequired[dict[str, Any]]
    side_effects: NotRequired[dict[str, Any]]
    uptime_s: NotRequired[dict[str, Any]]
    errors: NotRequired[dict[str, str]]
    dest: NotRequired[str]
    unverified: NotRequired[list[str]]


class SendResult(TypedDict):
    target: Any
    destination: Any
    kind: str
    packet_id: NotRequired[Any]
    delivery: NotRequired[str | None]
    ack_from: NotRequired[Any]
    reply: NotRequired[Any]
    waited_s: NotRequired[float]
    tx_confirmed: NotRequired[bool | None]
    tx_latency_s: NotRequired[float | None]
    tx_unconfirmed_reason: NotRequired[str]


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


class NamedDevice(TypedDict):
    name: str | None
    port: str | None
    attached: bool
    node_num: int | None
    model: str | None
    long_name: str | None
    short_name: str | None
    ble_address: str | None
    ble_name: str | None
    last_seen: float | None
    leased_to: str | None
    key: str


class DevicesResult(TypedDict):
    devices: list[NamedDevice]
    other_ports: NotRequired[
        Annotated[
            list[DeviceEntry],
            Field(description="with include_unknown: serial ports not seen as a radio"),
        ]
    ]


class LeaseInfo(TypedDict):
    device: str
    port: str
    holder: str
    host: str
    claimed_at: float
    expires_at: float


class ClaimResult(LeaseInfo):
    lease: str


class QueryResult(TypedDict):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class WaitForResult(TypedDict):
    matched: bool
    rows: list[list[Any]]
    columns: list[str]
    waited_s: float
    port: str


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
    target: NotRequired[Any]
    channels_imported: Annotated[int, Field(description="channels not DISABLED after the import")]
    channels_added: NotRequired[Annotated[int, Field(description="new channels, with add_only")]]


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


class InjectResult(TypedDict, total=False):
    """`inject` result: `id`, `queued` and `connected`, or `error` for an unknown session."""

    id: str
    queued: int
    connected: bool
    error: str


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


# ---------- simulated radio (`sim`, `sim_status`) ---------------------------


class SimSession(TypedDict, total=False):
    """One simulated radio session, as `sim(action="start")` and `sim_status` report it."""

    id: str
    capture: str
    listen: str
    connect: list[str]
    mode: str
    target_rate: float | None
    achieved_rate: float | None
    loop: bool
    hold: bool
    pass_complete: bool
    nodes: int
    channels: Any
    packets_total: int
    packets_sent: int
    injected: Annotated[int, Field(description="injected packets written to a connected client")]
    inject_queued: NotRequired[
        Annotated[int, Field(description="accepted injections waiting for a client to connect")]
    ]
    stats_sent: int
    duplicates_seen: int
    connected: bool
    client: Any
    ended: bool
    error: Any
    capture_span: dict[str, Any]
    uptime_s: float
    fuzz: Any
    mdns: Any
    local_metrics: Any


class SimResult(SimSession):
    """`sim` result: the started session, or `stopped` for `action="stop"`."""

    stopped: NotRequired[
        Annotated[list[str], Field(description="ids of the sessions a stop ended")]
    ]


class SimStatusResult(SimSession):
    """`sim_status` result: one session's fields, `sessions` for all, or `error` for a bad id."""

    sessions: NotRequired[list[SimSession]]
    presets: NotRequired[
        Annotated[dict[str, Any], Field(description="with fuzz_presets: {name: {category: rate}}")]
    ]
    usage: NotRequired[str]


# ---------- App driving -----------------------------------------------------


class AppResult(TypedDict):
    """`app` and `app_view` result."""

    platform: str
    action: str
    result: Annotated[Any, Field(description="what the backing tool returns; its shape varies")]


# ---------- background jobs, install and vendor ------------------------------


class JobListEntry(TypedDict, total=False):
    job_id: str
    kind: str
    label: str
    status: str
    elapsed_s: float


class JobResult(TypedDict, total=False):
    """`job` result: one job's state (plus the keys its kind's poller adds), or `jobs`."""

    jobs: Annotated[list[JobListEntry], Field(description="every job, when no id was given")]
    job_id: str
    kind: str
    label: str
    status: Annotated[str, Field(description="running, done, failed or canceled")]
    elapsed_s: float
    duration_s: float | None
    exit_code: int | None
    artifacts: list[Any]
    log_tail: Any
    log_path: str
    worker_error: Any


class VendorResult(TypedDict, total=False):
    """`vendor` result: the command's exit code and output; parsed keys vary by subcommand."""

    exit_code: int | None
    stdout: str
    stderr: str
    stdout_tail: Any
    stderr_tail: Any
    raw_stdout_tail: Any
    duration_s: float


class InstallResult(TypedDict, total=False):
    """`install` result: the flash job, the method it used and what the board reports after."""

    job_id: str
    method: Annotated[str, Field(description="the flasher used, e.g. esptool-ota, uf2, nrfutil")]
    exit_code: int | None
    stdout_tail: Any
    stderr_tail: Any
    back: Annotated[bool, Field(description="the board answered after the write")]
    firmware_version: str | None
    error: Any
