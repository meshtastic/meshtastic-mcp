# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Packet builders for scripted scenarios and live injection.

Construct the MeshPackets an app-feature test needs (a waypoint with a geofence,
a node position, a text, a NodeInfo) without hand-assembling protobufs. Used by
``replay_inject`` (push into a live session), ``Capture.from_events`` (a scripted
capture source), and directly in tests.

``append_fields`` lets a builder set proto fields the bundled ``meshtastic``
package predates — e.g. the Waypoint geofence fields (``geofence_radius`` #9,
``bounding_box`` #10, ``notify_on_enter/exit/favorites_only`` #11/12/13). The
wire format is forward-compatible, so the extra fields are appended as raw bytes
and a newer client decodes them.
"""

from __future__ import annotations

import struct
import time
from typing import Any

from meshtastic.protobuf import config_pb2, mesh_pb2

BROADCAST = 0xFFFFFFFF
_id_seed = int(time.time() * 1000)


def _next_id() -> int:
    global _id_seed
    _id_seed += 1
    return _id_seed & 0x7FFFFFFF


def li(deg: float) -> int:
    """Decimal degrees -> Meshtastic ``*_i`` integer (×1e7)."""
    return round(deg * 1e7)


# ── wire helpers (for fields the bundled proto predates) ─────────────────────
def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _sfixed32(field: int, val: int) -> bytes:
    return _tag(field, 5) + struct.pack("<i", val)


def append_fields(fields: dict[int, Any]) -> bytes:
    """Encode extra proto fields as raw wire bytes (append to a serialized msg).

    Value types: ``bool``/``int`` → varint; ``bytes`` → length-delimited (e.g. a
    sub-message). Concatenation with an existing serialized message merges them.
    """
    out = b""
    for field, val in sorted(fields.items()):
        if isinstance(val, bool):
            out += _tag(field, 0) + _varint(1 if val else 0)
        elif isinstance(val, int):
            out += _tag(field, 0) + _varint(val)
        elif isinstance(val, (bytes, bytearray)):
            out += _tag(field, 2) + _varint(len(val)) + bytes(val)
        else:
            raise TypeError(f"unsupported field {field} value type {type(val).__name__}")
    return out


def bounding_box(south: float, west: float, north: float, east: float) -> bytes:
    """Encode a Waypoint ``BoundingBox`` sub-message (fields west/south/east/north)."""
    return (
        _sfixed32(1, li(west))  # longitude_west_i
        + _sfixed32(2, li(south))  # latitude_south_i
        + _sfixed32(3, li(east))  # longitude_east_i
        + _sfixed32(4, li(north))  # latitude_north_i
    )


# ── decoded-payload builders ─────────────────────────────────────────────────
def _enum(enum_type: Any, name: str | None, default: int = 0) -> int:
    if not name:
        return default
    try:
        return enum_type.Value(str(name).strip().upper())
    except Exception:
        return default


def waypoint_payload(
    lat: float,
    lon: float,
    *,
    name: str = "",
    description: str = "",
    icon: int = 0,
    waypoint_id: int = 0,
    expire: int = 0,
    geofence_radius: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
    notify_on_enter: bool = False,
    notify_on_exit: bool = False,
    notify_favorites_only: bool = False,
) -> bytes:
    """A Waypoint payload, incl. the (newer) geofence fields when requested.

    ``bbox`` is ``(south, west, north, east)`` in decimal degrees. Geofence
    fields are appended as raw wire bytes (the bundled proto predates them).
    """
    w = mesh_pb2.Waypoint()
    w.id = waypoint_id or _next_id()
    w.latitude_i = li(lat)
    w.longitude_i = li(lon)
    w.expire = expire or (int(time.time()) + 86400)
    if name:
        w.name = name
    if description:
        w.description = description
    if icon:
        w.icon = icon
    extra: dict[int, Any] = {}
    if geofence_radius:
        extra[9] = int(geofence_radius)
    if bbox:
        extra[10] = bounding_box(*bbox)
    if notify_on_enter:
        extra[11] = True
    if notify_on_exit:
        extra[12] = True
    if notify_favorites_only:
        extra[13] = True
    return w.SerializeToString() + append_fields(extra)


def position_payload(
    lat: float,
    lon: float,
    *,
    altitude: int = 0,
    when: int = 0,
    sats: int = 9,
    precision_bits: int = 32,
) -> bytes:
    p = mesh_pb2.Position()
    p.latitude_i = li(lat)
    p.longitude_i = li(lon)
    if altitude:
        p.altitude = altitude
    p.time = when or int(time.time())
    p.sats_in_view = sats
    p.precision_bits = precision_bits
    return p.SerializeToString()


def nodeinfo_payload(
    node_id: str,
    *,
    long_name: str = "",
    short_name: str = "",
    hw_model: str = "",
    role: str = "CLIENT",
) -> bytes:
    u = mesh_pb2.User()
    u.id = node_id
    u.long_name = long_name or node_id
    u.short_name = short_name or node_id[-4:]
    u.hw_model = _enum(mesh_pb2.HardwareModel, hw_model)
    u.role = _enum(config_pb2.Config.DeviceConfig.Role, role)
    return u.SerializeToString()


# ── full MeshPacket assembly ─────────────────────────────────────────────────
def packet(
    portnum: int,
    payload: bytes,
    *,
    from_node: int,
    to_node: int = BROADCAST,
    channel_idx: int = 0,
    hop_limit: int = 3,
    want_ack: bool = False,
    rx_time: int | None = None,
) -> mesh_pb2.MeshPacket:
    mp = mesh_pb2.MeshPacket()
    setattr(mp, "from", from_node & 0xFFFFFFFF)
    mp.to = to_node & 0xFFFFFFFF
    mp.id = _next_id()
    mp.rx_time = rx_time if rx_time is not None else int(time.time())
    mp.hop_limit = hop_limit
    mp.hop_start = max(hop_limit, 3)
    mp.channel = channel_idx
    if want_ack:
        mp.want_ack = True
    mp.decoded.portnum = portnum
    mp.decoded.payload = payload
    return mp


# portnum constants used by the builders / inject tool
PORTNUM = {
    "text": 1,
    "position": 3,
    "nodeinfo": 4,
    "waypoint": 8,
}


def from_kind(
    kind: str,
    args: dict[str, Any],
    *,
    from_node: int,
    to_node: int = BROADCAST,
    channel_idx: int = 0,
) -> mesh_pb2.MeshPacket:
    """Build a MeshPacket from a high-level ``kind`` + ``args`` (the inject API).

    kinds: ``waypoint`` (lat, lon, name, geofence_radius, bbox, notify_on_enter,
    notify_on_exit, notify_favorites_only, icon), ``position`` (lat, lon),
    ``text`` (body), ``nodeinfo`` (id, long_name, short_name, hw_model, role),
    ``raw`` (portnum, payload_hex).
    """
    a = args or {}
    if kind == "waypoint":
        pl = waypoint_payload(
            a["lat"],
            a["lon"],
            name=a.get("name", ""),
            icon=a.get("icon", 0),
            geofence_radius=a.get("geofence_radius", 0),
            bbox=a.get("bbox"),
            notify_on_enter=a.get("notify_on_enter", False),
            notify_on_exit=a.get("notify_on_exit", False),
            notify_favorites_only=a.get("notify_favorites_only", False),
        )
        return packet(8, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx)
    if kind == "position":
        pl = position_payload(a["lat"], a["lon"], altitude=a.get("altitude", 0))
        return packet(3, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx)
    if kind == "text":
        return packet(
            1,
            str(a.get("body", "")).encode("utf-8"),
            from_node=from_node,
            to_node=to_node,
            channel_idx=channel_idx,
        )
    if kind == "nodeinfo":
        pl = nodeinfo_payload(
            a.get("id", f"!{from_node:08x}"),
            long_name=a.get("long_name", ""),
            short_name=a.get("short_name", ""),
            hw_model=a.get("hw_model", ""),
            role=a.get("role", "CLIENT"),
        )
        return packet(4, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx)
    if kind == "raw":
        return packet(
            int(a["portnum"]),
            bytes.fromhex(a.get("payload_hex", "")),
            from_node=from_node,
            to_node=to_node,
            channel_idx=channel_idx,
        )
    raise ValueError(f"unknown inject kind: {kind!r}")
