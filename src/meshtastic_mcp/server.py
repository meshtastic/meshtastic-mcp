# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The MCP server: the `FastMCP` app with every tool in `tools/` registered on it.

Importing it registers the tools, adds the toolset and annotation transforms, starts the
recorder and registers the resources and prompts. Tool hints come from the `_READ_ONLY` /
`_DESTRUCTIVE` / `_OPEN_WORLD` maps here, through `_AnnotationTransform`. Each tool and
helper is re-exported, so `server.<name>` still reaches it.
"""

from __future__ import annotations

import itertools
import logging
import math as math
import os
import re as re
import secrets as secrets
import tempfile as tempfile
import threading as threading
import time as time
from collections.abc import Callable as Callable
from collections.abc import Sequence
from datetime import UTC as UTC
from pathlib import Path as Path
from typing import Any as Any
from typing import cast as cast

import anyio as anyio
from fastmcp import Context as Context
from fastmcp import FastMCP as FastMCP
from fastmcp.exceptions import ToolError as ToolError
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp.types import ToolAnnotations

from . import admin as admin
from . import ble_sniffer as ble_sniffer
from . import boards as boards
from . import capabilities, info, toolsets
from . import config as config
from . import connection as connection
from . import devices as devices
from . import doctor as doctor_mod
from . import fixtures as fixtures
from . import flash as flash
from . import hw_tools as hw_tools
from . import jobs as jobs
from . import leases as leases
from . import log_query as log_query
from . import nrf52_bootloader as nrf52_bootloader
from . import recorder_query as recorder_query
from . import registry as registry
from . import rf_oracle as rf_oracle
from . import serial_session as serial_session
from . import vanity as vanity
from .call_log import CallLogMiddleware
from .retired_tools import RetiredToolMiddleware
from .recorder import get_recorder
from .replay import ReplayParams as ReplayParams
from .schemas import DeviceEntry as DeviceEntry
from .schemas import DeviceInfo as DeviceInfo
from .schemas import DoctorReport as DoctorReport
from .schemas import EventsWindow as EventsWindow
from .schemas import LogsWindow as LogsWindow
from .schemas import NodeEntry as NodeEntry
from .schemas import PacketsWindow as PacketsWindow
from .tools._app import CAPS, app
from .tools._app import _pkg_version as _pkg_version
from .tools._app import android_tool as android_tool
from .tools._app import app_plane_tool as app_plane_tool
from .tools._app import apple_tool as apple_tool
from .tools._app import ble_sniffer_tool as ble_sniffer_tool
from .tools._app import firmware_tool as firmware_tool
from .tools._app import sdk_tool as sdk_tool
from .tools._app import sdr_tool as sdr_tool
from .tools._app import tasks_mod as tasks_mod
from .tools._app import vanity_tool as vanity_tool
from .tools._shared import _capture_dir as _capture_dir
from .tools._shared import _confirmed_hw_call as _confirmed_hw_call
from .tools._shared import _off_loop as _off_loop
from .tools._shared import _output_tail as _output_tail
from .tools._shared import _raise_if_failed as _raise_if_failed
from .tools._shared import _run_job as _run_job
from .tools._shared import _to_markdown_table as _to_markdown_table
from .tools._shared import confirm_mod as confirm_mod
from .tools.android import _ANDROID_SCREENSHOT_DIR as _ANDROID_SCREENSHOT_DIR
from .tools.android import _ATAK_DRIVES as _ATAK_DRIVES
from .tools.android import _ATAK_FLEET as _ATAK_FLEET
from .tools.android import _ATAK_LOCK as _ATAK_LOCK
from .tools.android import _ATAK_UP as _ATAK_UP
from .tools.android import _atak_teardown as _atak_teardown
from .tools.android import android_clear_logcat as android_clear_logcat
from .tools.android import android_docs_fetch as android_docs_fetch
from .tools.android import android_docs_search as android_docs_search
from .tools.android import android_find_text as android_find_text
from .tools.android import android_poll_for_text as android_poll_for_text
from .tools.android import android_read_logcat as android_read_logcat
from .tools.android import android_render_compose_preview as android_render_compose_preview
from .tools.android import android_resolve as android_resolve
from .tools.android import android_screenshot as android_screenshot
from .tools.android import android_swipe as android_swipe
from .tools.android import android_tap as android_tap
from .tools.android import android_type_text as android_type_text
from .tools.android import android_ui_dump as android_ui_dump
from .tools.android import android_version_lookup as android_version_lookup
from .tools.android import atak_drive_route as atak_drive_route
from .tools.android import atak_drive_stop as atak_drive_stop
from .tools.android import atak_fleet_down as atak_fleet_down
from .tools.android import atak_fleet_status as atak_fleet_status
from .tools.android import atak_fleet_up as atak_fleet_up
from .tools.android import atak_share_item as atak_share_item
from .tools.app_drive import app_view as app_view
from .tools.apple import _IOS_SCREENSHOT_DIR as _IOS_SCREENSHOT_DIR
from .tools.apple import ios_boot as ios_boot
from .tools.apple import ios_list_simulators as ios_list_simulators
from .tools.apple import ios_open_url as ios_open_url
from .tools.apple import ios_screenshot as ios_screenshot
from .tools.apple import ios_tap as ios_tap
from .tools.apple import ios_type_text as ios_type_text
from .tools.apple import ios_ui_dump as ios_ui_dump
from .tools.apple import screenshot_set_poll as screenshot_set_poll
from .tools.apple import screenshot_set_start as screenshot_set_start
from .tools.core import _DISPLAY_CAPTURE_DIR as _DISPLAY_CAPTURE_DIR
from .tools.core import _capture_target as _capture_target
from .tools.core import _confirm_tx as _confirm_tx
from .tools.core import capture_display as capture_display
from .tools.core import claim as claim
from .tools.core import config_diff as config_diff
from .tools.core import config_snapshot as config_snapshot
from .tools.core import config_snapshot_mod as config_snapshot_mod
from .tools.core import config_snapshots_list as config_snapshots_list
from .tools.core import device_info as device_info
from .tools.core import doctor as doctor
from .tools.core import drive_display_poll as drive_display_poll
from .tools.core import drive_display_start as drive_display_start
from .tools.core import events_window as events_window
from .tools.core import factory_reset as factory_reset
from .tools.core import get_channel_url as get_channel_url
from .tools.core import get_config as get_config
from .tools.core import inject_frame as inject_frame
from .tools.core import inject_mod as inject_mod
from .tools.core import job as job
from .tools.core import list_devices as list_devices
from .tools.core import list_nodes as list_nodes
from .tools.core import logs_window as logs_window
from .tools.core import mark_event as mark_event
from .tools.core import packets_window as packets_window
from .tools.core import query as query
from .tools.core import reboot as reboot
from .tools.core import recorder_export as recorder_export
from .tools.core import recorder_pause as recorder_pause
from .tools.core import recorder_resume as recorder_resume
from .tools.core import recorder_status as recorder_status
from .tools.core import release as release
from .tools.core import send_input_event as send_input_event
from .tools.core import send_text as send_text
from .tools.core import set_channel_url as set_channel_url
from .tools.core import set_config as set_config
from .tools.core import set_debug_log_api as set_debug_log_api
from .tools.core import set_owner as set_owner
from .tools.core import shutdown as shutdown
from .tools.core import telemetry_timeline as telemetry_timeline
from .tools.core import triage_bundle as triage_bundle
from .tools.core import wait_for as wait_for
from .tools.firmware import build as build
from .tools.firmware import build_poll as build_poll
from .tools.firmware import build_start as build_start
from .tools.firmware import clean as clean
from .tools.firmware import erase_and_flash as erase_and_flash
from .tools.firmware import flash_poll as flash_poll
from .tools.firmware import flash_start as flash_start
from .tools.firmware import get_board as get_board
from .tools.firmware import list_boards as list_boards
from .tools.firmware import pio_flash as pio_flash
from .tools.firmware import push_fake_nodedb as push_fake_nodedb
from .tools.firmware import update_flash as update_flash
from .tools.firmware import userprefs_get as userprefs_get
from .tools.firmware import userprefs_manifest as userprefs_manifest
from .tools.firmware import userprefs_mod as userprefs_mod
from .tools.firmware import userprefs_reset as userprefs_reset
from .tools.firmware import userprefs_set as userprefs_set
from .tools.firmware import userprefs_testing_profile as userprefs_testing_profile
from .tools.hardware import _BY_ID_SERIAL as _BY_ID_SERIAL
from .tools.hardware import _SD_APP_START as _SD_APP_START
from .tools.hardware import _check_softdevice as _check_softdevice
from .tools.hardware import _resolve_uhubctl_target as _resolve_uhubctl_target
from .tools.hardware import _uf2_drive as _uf2_drive
from .tools.hardware import _uf2_result as _uf2_result
from .tools.hardware import _wait_for_uf2_drive as _wait_for_uf2_drive
from .tools.hardware import capture_screen as capture_screen
from .tools.hardware import esptool_chip_info as esptool_chip_info
from .tools.hardware import esptool_erase_flash as esptool_erase_flash
from .tools.hardware import esptool_raw as esptool_raw
from .tools.hardware import nrf52_backup_firmware as nrf52_backup_firmware
from .tools.hardware import nrf52_bootloader_info as nrf52_bootloader_info
from .tools.hardware import nrf52_enter_uf2 as nrf52_enter_uf2
from .tools.hardware import nrf52_factory_erase as nrf52_factory_erase
from .tools.hardware import nrf52_flash_uf2 as nrf52_flash_uf2
from .tools.hardware import nrfutil_dfu as nrfutil_dfu
from .tools.hardware import nrfutil_raw as nrfutil_raw
from .tools.hardware import picotool_info as picotool_info
from .tools.hardware import picotool_load as picotool_load
from .tools.hardware import picotool_raw as picotool_raw
from .tools.hardware import serial_close as serial_close
from .tools.hardware import serial_list as serial_list
from .tools.hardware import serial_open as serial_open
from .tools.hardware import serial_read as serial_read
from .tools.hardware import touch_1200bps as touch_1200bps
from .tools.hardware import uhubctl_cycle as uhubctl_cycle
from .tools.hardware import uhubctl_list as uhubctl_list
from .tools.hardware import uhubctl_power as uhubctl_power
from .tools.replay import _COT_LOCK as _COT_LOCK
from .tools.replay import _COT_RELAY as _COT_RELAY
from .tools.replay import _cot_data_dir as _cot_data_dir
from .tools.replay import _load_replay_capture as _load_replay_capture
from .tools.replay import _parse_iso_epoch as _parse_iso_epoch
from .tools.replay import _safe_session_name as _safe_session_name
from .tools.replay import cot_relay_start as cot_relay_start
from .tools.replay import cot_relay_status as cot_relay_status
from .tools.replay import cot_relay_stop as cot_relay_stop
from .tools.replay import get_replay_manager as get_replay_manager
from .tools.replay import inject as inject
from .tools.replay import replay_build as replay_build
from .tools.replay import replay_capture as replay_capture
from .tools.replay import replay_engine as replay_engine
from .tools.replay import replay_fuzz as replay_fuzz
from .tools.replay import replay_fuzz_presets as replay_fuzz_presets
from .tools.replay import replay_inject as replay_inject
from .tools.replay import replay_inject_beacon as replay_inject_beacon
from .tools.replay import replay_inject_client_notification as replay_inject_client_notification
from .tools.replay import replay_inject_fileinfo as replay_inject_fileinfo
from .tools.replay import replay_inject_traceroute as replay_inject_traceroute
from .tools.replay import replay_inject_waypoint as replay_inject_waypoint
from .tools.replay import replay_sim as replay_sim
from .tools.replay import replay_start as replay_start
from .tools.replay import replay_status as replay_status
from .tools.replay import replay_stop as replay_stop
from .tools.replay import sim as sim
from .tools.replay import sim_status as sim_status
from .tools.rf import ble_sniff_poll as ble_sniff_poll
from .tools.rf import ble_sniff_start as ble_sniff_start
from .tools.rf import ble_sniff_status as ble_sniff_status
from .tools.rf import ble_sniff_stop as ble_sniff_stop
from .tools.rf import pa_measure as pa_measure
from .tools.rf import pa_meter_status as pa_meter_status
from .tools.rf import pa_sweep as pa_sweep
from .tools.rf import pa_sweep_mod as pa_sweep_mod
from .tools.rf import rf_confirm_tx as rf_confirm_tx
from .tools.rf import rf_scan as rf_scan
from .tools.rf import vanity_apply as vanity_apply
from .tools.rf import vanity_grind_poll as vanity_grind_poll
from .tools.rf import vanity_grind_start as vanity_grind_start
from .tools.rf import vanity_grind_stop as vanity_grind_stop
from .tools.rf import vanity_preview as vanity_preview
from .tools.sdk import sdk_cli_mod as sdk_cli_mod
from .tools.sdk import sdk_device_info as sdk_device_info
from .tools.sdk import sdk_list_nodes as sdk_list_nodes
from .tools.sdk import sdk_send_text as sdk_send_text
from .tools.sdk import sdk_status as sdk_status

log = logging.getLogger(__name__)


app.add_transform(toolsets.ToolsetTransform(os.environ, lambda: _READ_ONLY))
app.add_middleware(CallLogMiddleware())
app.add_middleware(RetiredToolMiddleware())


def _start_recorder() -> None:
    # Persistent device-log capture. Starts on first import — pubsub fan-out
    # is process-global, so subscribing here captures every active interface
    # (whether opened by an MCP tool, a pytest fixture, or a serial_session).
    # Files land in mcp-server/.mtlog/ (gitignored). See recorder/recorder.py
    # for the full design. Recorder startup is best-effort: an unwritable
    # log dir or pubsub mismatch should not take the MCP server down.
    try:
        get_recorder().start()
    except Exception as exc:
        log.warning("Failed to start persistent recorder: %s", exc)


_start_recorder()


# Android-coupled tools, gated by `android_tool` on the android capability.
_ANDROID_TOOLS = (
    "android_docs_search",
    "android_docs_fetch",
    "android_version_lookup",
    "android_render_compose_preview",
    "android_ui_dump",
    "android_screenshot",
    "android_resolve",
    "android_tap",
    "android_swipe",
    "android_type_text",
    "android_find_text",
    "android_poll_for_text",
    "android_clear_logcat",
    "android_read_logcat",
    "atak_fleet_up",
    "atak_fleet_status",
    "atak_fleet_down",
    "atak_drive_route",
    "atak_drive_stop",
    "atak_share_item",
)

# Apple-coupled tools, gated by `apple_tool` on the apple capability (xcrun).
_APPLE_TOOLS = (
    "ios_list_simulators",
    "ios_boot",
    "ios_screenshot",
    "ios_open_url",
    "ios_ui_dump",
    "ios_tap",
    "ios_type_text",
)

# SDR-coupled tools, gated by `sdr_tool` on the sdr capability.
_SDR_TOOLS = (
    "rf_scan",
    "rf_confirm_tx",
)

# BLE-sniffer-coupled tools, gated by `ble_sniffer_tool`. `ble_sniff_status` is
# deliberately absent — it is core.
_BLE_SNIFFER_TOOLS = (
    "ble_sniff_start",
    "ble_sniff_poll",
    "ble_sniff_stop",
)

# mvgrind-coupled tools, gated by `vanity_tool` on the mvgrind capability.
_VANITY_TOOLS = (
    "vanity_grind_start",
    "vanity_grind_poll",
    "vanity_grind_stop",
)

# Firmware-coupled tools, gated by `firmware_tool` on the firmware capability.
_FIRMWARE_TOOLS = (
    "list_boards",
    "get_board",
    "build_start",
    "build_poll",
    "build",
    "clean",
    "pio_flash",
    "flash_start",
    "flash_poll",
    "erase_and_flash",
    "update_flash",
    "userprefs_manifest",
    "userprefs_get",
    "userprefs_set",
    "userprefs_reset",
    "userprefs_testing_profile",
    "push_fake_nodedb",
)


def _log_capabilities() -> None:
    try:
        log.info("meshtastic-mcp capabilities active: %s", CAPS.summary())
        if not CAPS.android:
            log.info(
                "android capability inactive: %d android tools not registered: %s",
                len(_ANDROID_TOOLS),
                ", ".join(_ANDROID_TOOLS),
            )
        if not CAPS.apple:
            log.info(
                "apple capability inactive: %d iOS Simulator tools not registered: %s",
                len(_APPLE_TOOLS),
                ", ".join(_APPLE_TOOLS),
            )
        if not CAPS.firmware:
            log.info(
                "firmware capability inactive: %d build/flash tools not registered "
                "(set MESHTASTIC_FIRMWARE_ROOT to enable): %s",
                len(_FIRMWARE_TOOLS),
                ", ".join(_FIRMWARE_TOOLS),
            )
        if not CAPS.mvgrind:
            log.info(
                "mvgrind capability inactive: %d vanity-grind tools not registered "
                "(build https://github.com/miketweaver/mvgrind, or set "
                "$MESHTASTIC_MCP_MVGRIND): %s — vanity_preview/vanity_apply still work",
                len(_VANITY_TOOLS),
                ", ".join(_VANITY_TOOLS),
            )
        if not CAPS.sdr:
            log.info(
                "sdr capability inactive: %d RF-compliance tools not registered "
                "(install the 'sdr' extra + librtlsdr + an RTL-SDR to enable): %s",
                len(_SDR_TOOLS),
                ", ".join(_SDR_TOOLS),
            )
        if not CAPS.ble_sniffer:
            log.info(
                "ble_sniffer capability inactive: %d BLE-capture tools not registered "
                "(attach an nRF52840 Dongle running the nRF Sniffer for Bluetooth LE "
                "firmware to enable): %s — ble_sniff_status still works",
                len(_BLE_SNIFFER_TOOLS),
                ", ".join(_BLE_SNIFFER_TOOLS),
            )
    except Exception as exc:  # never fail startup over a capability probe
        log.warning("capability detection failed: %s", exc)


_log_capabilities()


# ---------- MCP tool annotations (modern hint metadata) -------------------
# Annotations let clients reason about each tool without calling it: surface
# read-only vs destructive in the UI, auto-approve safe reads, warn before
# device-mutating actions, etc. These COMPLEMENT (not replace) the `confirm=`
# gate on destructive tools. Applied post-registration so we don't decorate 53
# call sites by hand. See https://modelcontextprotocol.io (Tool annotations).

# Read-only: observe without changing device/host state.
_READ_ONLY: set[str] = {
    "doctor",
    "nrf52_bootloader_info",  # reads INFO_UF2.TXT off a mounted bootloader drive
    "nrf52_backup_firmware",  # copies CURRENT.UF2 off the drive; writes only the data dir
    "android_docs_search",
    "android_docs_fetch",
    "android_version_lookup",
    "android_render_compose_preview",
    "android_ui_dump",
    "android_resolve",
    "android_find_text",
    "android_poll_for_text",
    "android_read_logcat",  # see _OPEN_WORLD note: may echo remote-node content
    "app_view",  # the read-only actions of android_*, ios_* and the desktop server
    "ios_list_simulators",
    "ios_ui_dump",
    "screenshot_set_poll",  # reads job state; never mutates
    "triage_bundle",
    "list_devices",
    "list_boards",
    "get_board",
    "build_poll",  # reads background-build state; never mutates
    "flash_poll",  # reads background-flash state; never mutates
    "vanity_preview",  # pure key -> id/colour derivation; no device, no host state
    "vanity_grind_poll",  # reads grind-job state; never mutates
    "drive_display_poll",  # reads drive-job state; never touches the device
    "job",  # reads any job's state through its kind's poller; never mutates
    "devices",  # reads the name registry and enumerates ports; opens none
    "serial_list",
    "serial_read",  # reads buffered bytes; no write side-effect
    "inspect",
    "device_info",
    "list_nodes",
    "get_config",
    "get_channel_url",
    "config_snapshots_list",  # lists saved snapshots; no device/host mutation
    "config_diff",  # reads snapshots/live config; no mutation
    "uhubctl_list",
    "esptool_chip_info",
    "picotool_info",
    "userprefs_manifest",
    "userprefs_get",
    "logs_window",
    "telemetry_timeline",
    "packets_window",
    "events_window",
    "query",
    "wait_for",  # holds a connection open while it watches; changes nothing
    "recorder_status",
    "replay_status",  # reads replay-session run-state; never mutates
    "sim_status",  # reads simulated-session run-state; never mutates
    "cot_relay_status",  # reads relay run-state (peers/counts); never mutates
    "atak_fleet_status",  # reads fleet bring-up / drive progress; never mutates
    "replay_fuzz_presets",  # static catalog of fuzz presets; no state
    "capture_screen",
    "rf_scan",  # passive SDR capture; no device/host mutation
    "pa_meter_status",  # reads the power meter (version/stored freq/live dBm); no state change
    "ble_sniff_status",  # enumerates USB + resolves a binary; no capture, no state change
    "ble_sniff_poll",  # reads capture-job state + parses the pcap on disk
    "sdk_status",  # reports SDK-CLI bridge availability; no mutation
    "sdk_device_info",  # reads device snapshot via the Kotlin SDK CLI; no mutation
    "sdk_list_nodes",  # reads the device node DB via the Kotlin SDK CLI; no mutation
}
# Destructive: irreversible or device-state-mutating (most are confirm-gated too).
_DESTRUCTIVE = {
    "nrf52_enter_uf2",  # reboots the node into its bootloader
    "nrf52_flash_uf2",  # replaces the installed firmware
    "install",  # replaces the installed firmware, and with erase the whole flash
    "nrf52_factory_erase",  # wipes config, node DB and keys
    "build_start",  # launches a pio subprocess; cannot be undone mid-flight
    "flash_start",  # launches a pio upload subprocess
    "vanity_grind_start",  # spawns a GPU grinder; writes private-key material to disk
    "vanity_grind_stop",  # terminates that subprocess
    "ble_sniff_start",  # spawns a sniffer subprocess, holds its serial port, writes a pcap
    "ble_sniff_stop",  # terminates that subprocess
    "vanity_apply",  # replaces the node identity (NodeNum + keypair); not reversible
    "build",
    "clean",
    "pio_flash",
    "erase_and_flash",
    "update_flash",
    "touch_1200bps",
    "configure",  # writes config; reversible through the snapshot it returns
    "admin",  # restarts, powers off or resets a radio
    "set_owner",
    "set_config",
    "set_channel_url",
    "set_debug_log_api",
    "send",  # injects a mesh packet; cannot be recalled
    "send_text",  # injects a mesh packet; cannot be recalled
    "inject_frame",  # injects a forged frame into the RX pipeline; cannot be recalled
    "rf_confirm_tx",  # calls send_text internally; injects a mesh packet
    # Selects the meter's active calibration curve (set_freq_mhz) — a transient,
    # non-persisted instrument state change, so not read-only. Idempotent (same
    # band -> same curve) and harmless to any DUT; the set_config-style pattern.
    "pa_measure",
    "pa_sweep",  # writes lora.tx_power, keys TX, may reboot the node
    # Presses real keys; Enter in a focused MUI text field sends a mesh message.
    "drive_display_start",
    # Drives the device (one-shot mirror verb) and overwrites the PNG at its
    # fixed path — the same shape as android_screenshot below.
    "capture_display",
    "send_input_event",  # drives device button/GPIO; side-effect on hardware
    "android_screenshot",  # writes a PNG to the host filesystem
    "android_tap",  # drives device input; side-effect on the running app
    "app",  # android_tap/ios_tap and friends, plus desktop connect and send_text
    "android_swipe",
    "android_type_text",
    "android_clear_logcat",  # mutates device log buffer state
    "ios_boot",  # boots a simulator; host state change
    "ios_screenshot",  # writes a PNG; may switch appearance / override the status bar
    "ios_open_url",  # opens a URL in the running app (a connect link starts a connection)
    "ios_tap",  # drives simulator input
    "ios_type_text",
    # Starts replay nodes, switches both apps onto them, drives navigation, writes PNGs.
    "screenshot_set_start",
    "reboot",
    "shutdown",
    "factory_reset",
    "mark_event",  # writes to events.jsonl
    "recorder_pause",  # mutates recorder run-state
    "recorder_resume",  # mutates recorder run-state
    "serial_open",  # acquires the exclusive port lock
    "serial_close",  # releases the port lock / tears down session
    "userprefs_set",
    "userprefs_reset",
    "userprefs_testing_profile",
    "uhubctl_power",
    "uhubctl_cycle",
    "esptool_erase_flash",
    "esptool_raw",
    "nrfutil_dfu",
    "nrfutil_raw",
    "picotool_load",
    "picotool_raw",
    "vendor",  # runs esptool, nrfutil or picotool; erases and writes flash
    "push_fake_nodedb",
    # Writes/overwrites <stream>.jsonl under a client-supplied dest_dir on the
    # host filesystem — surface it so clients don't treat it as a benign read.
    "recorder_export",
    # Writes a snapshot JSON to the host filesystem (and reads the device).
    "config_snapshot",
    # Bind a TCP listener and serve a simulated mesh to a connecting app.
    "replay_start",
    "replay_stop",
    "sim",  # starts a simulated mesh listener, or stops sessions
    # Bind a TCP listener (CoT relay) / write captures to the host filesystem.
    "cot_relay_start",
    "cot_relay_stop",
    # Clone + boot emulators, install/provision ATAK, drive device GPS.
    "atak_fleet_up",
    "atak_fleet_down",
    "atak_drive_route",
    "atak_drive_stop",
    "atak_share_item",  # drives ATAK UI; broadcasts CoT to every connected peer
    "inject",  # emits packets or FromRadio messages onto the live replay connection
    "replay_inject",  # emits packets onto the live connection
    "replay_inject_beacon",  # emits a MESH_BEACON_APP packet
    "replay_inject_client_notification",  # emits a ClientNotification FromRadio to the client
    "replay_inject_fileinfo",  # emits a FileInfo FromRadio onto the live connection
    "replay_inject_traceroute",  # emits a TRACEROUTE_APP RouteDiscovery packet
    "replay_inject_waypoint",  # emits a WAYPOINT_APP packet (with optional geofence)
    "sdk_send_text",  # injects a mesh packet via the Kotlin SDK CLI; cannot be recalled
}
# Writes that are neither read-only nor destructive: they change host state another call
# can undo, and touch no device.
_ADDITIVE_WRITES = {
    "claim",  # takes a device lease; `release` or the TTL ends it
    "release",  # ends the caller's own lease
}
# Idempotent writes: calling with identical args leaves the device in identical
# state. Clients can safely retry these on timeout without side-effects.
_IDEMPOTENT_WRITES = {
    "admin",
    "configure",
    "set_config",
    "set_owner",
    "set_channel_url",
    "userprefs_set",
    "pa_measure",  # re-selecting the same band leaves the meter in the same state
}
# Open-world: interacts with an external device, radio mesh, or host hardware.
# Also includes tools whose OUTPUT may contain untrusted content sourced from
# remote mesh nodes (e.g. packet payloads, log lines) — relevant to the
# lethal-trifecta prompt-injection risk. See SECURITY.md.
_OPEN_WORLD = {
    "android_docs_search",  # queries the live Android Knowledge Base
    "android_docs_fetch",
    "android_version_lookup",  # queries live maven/Android version metadata
    "list_devices",
    "inspect",  # node names in the node DB are chosen by whoever owns each radio
    "device_info",
    "list_nodes",
    "get_config",
    "set_config",
    "get_channel_url",
    "set_channel_url",
    "config_snapshot",  # reads live device config
    "config_diff",  # may read live device config (name_b=None)
    "vanity_apply",  # writes the security config to a live device and reboots it
    "set_owner",
    "set_debug_log_api",
    "send",
    "send_text",
    "inject_frame",
    "reboot",
    "shutdown",
    "factory_reset",
    "send_input_event",
    "capture_screen",
    # The device's own screen renders mesh-sourced text — a remote node's
    # long_name, a received message — so a capture can carry an injection
    # payload exactly like android_screenshot. Untrusted content, one hop
    # closer to the source.
    "capture_display",
    "drive_display_start",
    "drive_display_poll",  # returns the PNG paths of those same screens
    "touch_1200bps",
    "pio_flash",
    "flash_start",
    "erase_and_flash",
    "update_flash",
    "serial_open",
    "serial_read",
    "uhubctl_list",
    "uhubctl_power",
    "uhubctl_cycle",
    "esptool_chip_info",
    "esptool_erase_flash",
    "esptool_raw",
    "nrfutil_dfu",
    "nrfutil_raw",
    "picotool_info",
    "picotool_load",
    "picotool_raw",
    # Reach the board's bootloader through its USB drive (or, for enter_uf2, its phone API).
    "nrf52_enter_uf2",
    "nrf52_bootloader_info",
    "nrf52_backup_firmware",
    "nrf52_flash_uf2",
    "nrf52_factory_erase",
    "push_fake_nodedb",
    # Talk to the external ImmersionRC power meter over USB; pa_sweep also drives
    # the node's TX onto the mesh.
    "pa_meter_status",
    "pa_measure",
    "pa_sweep",
    # Drive an external USB radio (the nRF Sniffer dongle). `ble_sniff_poll` and
    # `ble_sniff_stop` additionally return BLE device names, which are arbitrary
    # text chosen by whoever owns any radio in range — untrusted input on the
    # same footing as logs_window (lethal-trifecta leg 2).
    "ble_sniff_status",
    "ble_sniff_start",
    "ble_sniff_poll",
    "ble_sniff_stop",
    "job",  # returns a BLE capture's names, the same as ble_sniff_poll
    # Return user-authored content from remote mesh nodes — untrusted input
    # that can carry prompt-injection payloads (lethal-trifecta leg 2).
    "logs_window",
    "packets_window",
    "query",
    "wait_for",
    # UI/log content sourced from the running app, which can echo remote
    # mesh-node data (node names, text messages) — same lethal-trifecta
    # concern as logs_window/packets_window.
    "android_ui_dump",
    "android_screenshot",
    "android_read_logcat",
    "app_view",  # the same app UI, logs and node list, whichever platform
    "app",  # drives the app and can send a mesh message
    # Drive/query a real Android device over adb — external hardware, same
    # reason as send_input_event/capture_screen above (not an untrusted-
    # content concern like the three above; see AGENTS.md/SECURITY.md).
    "android_tap",
    "android_swipe",
    "android_type_text",
    "android_clear_logcat",
    "android_find_text",
    "android_poll_for_text",
    "android_resolve",
    # Boot/drive emulators over adb (external processes/hardware). The relay's
    # captured CoT is remote-authored TAK content (untrusted, prompt-injection
    # leg 2 — a marker callsign or GeoChat body reaches cot_relay_status output).
    "cot_relay_start",
    "cot_relay_status",
    "atak_fleet_up",
    "atak_fleet_status",
    "atak_fleet_down",
    "atak_drive_route",
    "atak_drive_stop",
    "atak_share_item",
}


# Display-name overrides for tools where name.replace("_", " ").title() is poor.
_TITLE_OVERRIDES: dict[str, str] = {
    "build_start": "Build Firmware (Async Start)",
    "build_poll": "Build Firmware (Poll Status)",
    "flash_start": "Flash Firmware (Async Start)",
    "flash_poll": "Flash Firmware (Poll Status)",
    "pio_flash": "PlatformIO Flash",
    "erase_and_flash": "Erase and Factory Flash (ESP32)",
    "update_flash": "OTA App-Partition Flash (ESP32)",
    "esptool_chip_info": "ESPTool Chip Info",
    "esptool_erase_flash": "ESPTool Erase Flash",
    "esptool_raw": "ESPTool Raw Pass-Through",
    "nrfutil_dfu": "nRF DFU Flash",
    "nrfutil_raw": "nRFUtil Raw Pass-Through",
    "picotool_info": "Picotool Info",
    "picotool_load": "Picotool Load UF2",
    "picotool_raw": "Picotool Raw Pass-Through",
    "vendor": "Vendor Flash Tool Pass-Through",
    "nrf52_enter_uf2": "nRF52 Enter UF2 Mode",
    "nrf52_bootloader_info": "nRF52 Bootloader Info",
    "nrf52_backup_firmware": "nRF52 Back Up Firmware",
    "nrf52_flash_uf2": "nRF52 Flash UF2",
    "nrf52_factory_erase": "nRF52 Factory Erase",
    "uhubctl_list": "USB Hub List",
    "uhubctl_power": "USB Hub Port Power",
    "uhubctl_cycle": "USB Hub Port Cycle",
    "pa_meter_status": "PA Meter Status",
    "pa_measure": "PA Meter Measure",
    "pa_sweep": "PA Power Sweep (Closed-Loop)",
    "ble_sniff_status": "BLE Sniffer Status",
    "ble_sniff_start": "BLE Capture (Async Start)",
    "ble_sniff_poll": "BLE Capture (Poll Status)",
    "ble_sniff_stop": "BLE Capture (Stop)",
    "vanity_preview": "Vanity Identity Preview",
    "vanity_grind_start": "Vanity Grind (Async Start)",
    "vanity_grind_poll": "Vanity Grind (Poll Status)",
    "vanity_grind_stop": "Vanity Grind (Stop)",
    "vanity_apply": "Adopt Vanity Identity",
}


def _annotations_for(name: str) -> ToolAnnotations:
    read_only = name in _READ_ONLY
    return ToolAnnotations(
        title=_TITLE_OVERRIDES.get(name) or name.replace("_", " ").title(),
        read_only_hint=read_only,
        destructive_hint=name in _DESTRUCTIVE,
        idempotent_hint=read_only or name in _IDEMPOTENT_WRITES,
        open_world_hint=name in _OPEN_WORLD,
    )


class _AnnotationTransform(Transform):
    """Stamp the classification maps' hints onto every tool the server lists or resolves."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t.model_copy(update={"annotations": _annotations_for(t.name)}) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None:
            return None
        return tool.model_copy(update={"annotations": _annotations_for(tool.name)})


# Tools whose `port` is not a device: a recorder filter, a listen port, a hub port, or the
# device a lease is being taken on.
_PORT_NOT_A_DEVICE = frozenset(
    {
        "claim",
        "cot_relay_start",
        "ios_open_url",
        "logs_window",
        "replay_start",
        "sim",
        "telemetry_timeline",
        "uhubctl_cycle",
        "uhubctl_power",
    }
)


# Tools that can find a board on their own when no port is given, so they must name one while
# another session holds a lease. Their `args` may carry the port instead.
_FINDS_ITS_OWN_BOARD = frozenset(
    {
        "esptool_raw",
        "nrf52_factory_erase",
        "nrf52_flash_uf2",
        "nrfutil_raw",
        "picotool_load",
        "picotool_raw",
        "vendor",
    }
)
_PORT_FLAGS = ("--port", "-p")


def _port_in_args(args: Any) -> str | None:
    """The one port `args` names; a tool takes the last of several, so two are refused."""
    if not isinstance(args, list):
        return None
    found = {
        value
        for flag, value in itertools.pairwise(args)
        if flag in _PORT_FLAGS and isinstance(value, str)
    }
    found |= {
        a.split("=", 1)[1]
        for a in args
        if isinstance(a, str) and a.startswith(tuple(f + "=" for f in _PORT_FLAGS))
    }
    if len(found) > 1:
        raise ToolError(f"args name more than one port ({', '.join(sorted(found))}); pass one.")
    return next(iter(found), None)


def _takes_device_port(tool: Tool) -> bool:
    props = (tool.parameters or {}).get("properties", {})
    if tool.name in _FINDS_ITS_OWN_BOARD:
        return True
    return ("port" in props or "node" in props) and tool.name not in _PORT_NOT_A_DEVICE


async def _with_lease_for(tool_name: str, lease: str | None, kwargs: dict[str, Any]) -> Any:
    from fastmcp.tools.tool_transform import forward

    port = kwargs.get("node", kwargs.get("port")) or _port_in_args(kwargs.get("args"))
    try:
        # A BLE radio has no lease key until the registry records its BLE identity.
        resolved = None if (port or "").startswith("ble:") else connection.resolve_port(port)
    except connection.ConnectionError:
        resolved = None  # the tool reports the missing or ambiguous port itself
    try:
        if resolved is not None:
            await anyio.to_thread.run_sync(leases.check, resolved, lease)
        elif not port and tool_name in _FINDS_ITS_OWN_BOARD:
            await anyio.to_thread.run_sync(leases.check_any, lease)
    except leases.LeaseError as exc:
        raise ToolError(str(exc)) from exc
    token = leases.CURRENT.set(lease)
    try:
        return await forward(**kwargs)
    finally:
        leases.CURRENT.reset(token)


_LEASE_ARG_DOC = (
    "The handle from `claim` while you hold this device; omit for a device nobody has claimed."
)


def _lease_aware(tool: Tool) -> Tool:
    if not _takes_device_port(tool):
        return tool
    name = tool.name

    async def _with_lease(lease: str | None = None, **kwargs: Any) -> Any:
        return await _with_lease_for(name, lease, kwargs)

    wrapped = Tool.from_tool(tool, transform_fn=_with_lease)
    props = dict(wrapped.parameters.get("properties", {}))
    if "lease" in props:
        props["lease"] = {**props["lease"], "description": _LEASE_ARG_DOC}
        wrapped = wrapped.model_copy(
            update={"parameters": {**wrapped.parameters, "properties": props}}
        )
    return wrapped


class _LeaseTransform(Transform):
    """Give every device tool a `lease` argument and refuse a call on someone else's lease."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_lease_aware(t) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return None if tool is None else _lease_aware(tool)


app.add_transform(_LeaseTransform())
app.add_transform(_AnnotationTransform())


# ---------- MCP resources (readable context, no tool round-trip) -----------
@app.resource(
    "meshtastic://doctor",
    name="Environment doctor report",
    mime_type="application/json",
)
def _resource_doctor() -> str:
    """Live capability + dependency report (same data as the `doctor` tool)."""
    import json

    return json.dumps(doctor_mod.run().to_dict(), indent=2)


@app.resource(
    "meshtastic://capabilities",
    name="Active capabilities",
    mime_type="text/plain",
)
def _resource_capabilities() -> str:
    """One-line summary of which capability groups are active on this host."""
    return capabilities.detect().summary()


@app.resource(
    "meshtastic://recorder/schema",
    name="Recorder query schema",
    mime_type="application/json",
)
def _resource_recorder_schema() -> str:
    """The views the `query` tool reads, with each column's DuckDB type."""
    import json

    return json.dumps(recorder_query.schema(), indent=2)


@app.resource(
    "meshtastic://device/info",
    name="Live device info",
    mime_type="application/json",
)
def _resource_device_info() -> str:
    """Live device summary (firmware/region/node id) for the auto-selected port.

    Reads the single connected device. For multi-device hosts use the
    `device_info` tool with an explicit port instead.
    """
    import json

    try:
        return json.dumps(info.device_info(port=None), indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://device/nodes",
    name="Live mesh node database",
    mime_type="application/json",
)
def _resource_device_nodes() -> str:
    """Live node DB (local node + peers) for the auto-selected port."""
    import json

    try:
        return json.dumps(info.list_nodes(port=None), indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@app.resource(
    "meshtastic://e2e/{loop}",
    name="e2e loop recipe",
    mime_type="text/markdown",
)
def _resource_e2e_loop(loop: str) -> str:
    """A bundled meshtastic-e2e reference doc (e.g. loop-inbound, harness, topology)."""
    import pathlib

    base = pathlib.Path(__file__).parent / "skills" / "meshtastic-e2e" / "references"
    name = loop if loop.endswith(".md") else f"{loop}.md"
    path = (base / name).resolve()
    if not path.is_file() or base.resolve() not in path.parents:
        avail = sorted(p.stem for p in base.glob("*.md"))
        return f"unknown loop {loop!r}. available: {', '.join(avail)}"
    return path.read_text(encoding="utf-8")


# ---------- MCP prompts (pre-baked agent workflows) ------------------------
@app.prompt(title="Triage an e2e failure")
def triage_e2e_failure(token: str = "", deadline_iso: str = "") -> str:
    """Guide a dual-plane root-cause analysis of a failed e2e loop."""
    return (
        "An e2e loop FAILED. Find the root cause by correlating the two planes:\n"
        f"- token: {token or '<the unique marker token>'}\n"
        f"- deadline: {deadline_iso or '<wall-clock of the assertion>'}\n\n"
        "1. Device plane (source of truth): call `packets_window` and `logs_window` for the\n"
        "   window around the deadline. Did a TEXT_MESSAGE_APP carrying the token actually\n"
        "   reach the wire? Check for NAK/err=5 (MAX_RETRANSMIT) on broadcasts.\n"
        "2. App plane: inspect the app `layout`/`screen capture` at the deadline. Was the app\n"
        "   connected (Subscribed)? On the right screen? Was the token rendered but missed?\n"
        "3. Align by shared epoch timestamps. Classify: never-sent / sent-not-received /\n"
        "   received-not-rendered / rendered-not-asserted.\n"
        "4. Emit a one-line root-cause hypothesis + the minimal repro. See meshtastic://e2e/harness."
    )


@app.prompt(title="Bring up a device")
def bringup_device(port: str = "", region: str = "US") -> str:
    """Connect to a Meshtastic device and verify a healthy baseline."""
    return (
        f"Bring up the Meshtastic device{' at ' + port if port else ''}:\n"
        "1. `list_devices` to find it (or use the given port / a tcp://host:port).\n"
        "2. `device_info` for firmware version + node identity; `list_nodes` for the DB.\n"
        f"3. Confirm region is set ({region}); if unset, `get_config('lora')` then `set_config`.\n"
        "4. `send` a text marker to ^all and confirm it leaves on the wire.\n"
        "Report firmware version, region, node count, and any config that looks off."
    )


@app.prompt(title="Run the inbound e2e loop")
def inbound_loop() -> str:
    """Drive the device->app inbound loop and report a verdict."""
    return (
        "Run the inbound (device->app) e2e loop. Read meshtastic://e2e/loop-inbound and\n"
        "meshtastic://e2e/harness first. Bring up the virtual mesh, connect the app over TCP,\n"
        "broadcast a unique marker token from the tester, and assert it renders in the app.\n"
        "Prefer a journey (meshtastic://e2e/journeys) over hardcoded taps. Respect the hard\n"
        "rules: unique token, bounded polling, the recorder is the device-side oracle. Emit\n"
        "`LOOP inbound PASS|FAIL token=... latency=...`."
    )


@app.prompt(title="Compare two firmware versions")
def compare_firmware(ref_a: str = "", ref_b: str = "") -> str:
    """Assess the app-facing behavioral delta between two firmware refs (impact agent)."""
    return (
        f"Compare firmware {ref_a or '<ref A / baseline>'} vs {ref_b or '<ref B / candidate>'} "
        "for app-facing impact:\n"
        "1. Build each: `scripts/build_meshtasticd.sh --env native-macos --ref <ref>` (records\n"
        "   the resolved sha). Or use the CI `firmware_ref` input.\n"
        "2. For each build, run the inbound + outbound + node-sync loops against the SAME app\n"
        "   build (pin it), capturing the device-plane recorder windows + the app verdicts.\n"
        "3. Diff: protobuf/config changes, new/renamed packet fields, timing/latency, any loop\n"
        "   that flips PASS<->FAIL. Use `android_docs_search` for migration notes where relevant.\n"
        "4. Report: a per-loop verdict table (A vs B) + a short app-facing-impact summary and the\n"
        "   two resolved shas. Flag anything that would break the current app."
    )
