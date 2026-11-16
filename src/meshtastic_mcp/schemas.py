# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Result shapes the most-used tools advertise as their MCP `outputSchema`.

Clients validate a tool's result against this schema, so a field's type must hold for every value
the tool can return. Keys are optional and unlisted keys are allowed; a field whose type varies
stays `Any`.
"""

from __future__ import annotations

import sys
from typing import Any

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
