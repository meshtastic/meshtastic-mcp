# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Packet builders for scripted scenarios and live injection.

Construct the MeshPackets an app-feature test needs (a waypoint with a geofence,
a node position, a text, a NodeInfo) without hand-assembling protobufs. Used by
``replay_inject`` (push into a live session), ``Capture.from_events`` (a scripted
capture source), and directly in tests.

``append_fields`` encodes proto fields as raw wire bytes, for a field a client
build may not know yet; the wire format is forward-compatible, so a newer client
decodes them.

Packets follow the 3.0 schema: positions in the full-precision client-link form,
a User without id, hardware models as packed registry numbers.
"""

from __future__ import annotations

import struct
import time
from typing import Any

from meshtastic.protobuf import (
    api_pb2,
    common_pb2,
    mesh_beacon_pb2,
    packet_pb2,
    portnums_pb2,
    telemetry_pb2,
    wire_pb2,
)
from meshtastic.util import hw_model_number

BROADCAST = 0xFFFFFFFF

PortNum = portnums_pb2.PortNum
MESH_BEACON_APP = PortNum.MESH_BEACON_APP
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


def hw_model_value(value: str | int | None) -> int:
    """A packed hardware model ``(vendor_id << 8) | device_id``, from its number or its
    hardware-registry slug (e.g. ``"HELTEC_V3"``); 0 when unknown."""
    if isinstance(value, int):
        return value
    return hw_model_number(str(value).strip().upper()) if value else 0


def modem_preset(name: str | None, default: int = 0) -> int:
    """A ModemPreset from its name, with or without the 3.0 ``MODEM_`` prefix."""
    if not name:
        return default
    n = str(name).strip().upper()
    return _enum(common_pb2.ModemPreset, n if n.startswith("MODEM_") else f"MODEM_{n}", default)


def region_code(name: str | None, default: int = 0) -> int:
    """A RegionCode from its name, with or without the 3.0 ``REGION_`` prefix."""
    if not name:
        return default
    n = str(name).strip().upper()
    return _enum(common_pb2.RegionCode, n if n.startswith("REGION_") else f"REGION_{n}", default)


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
    """A Waypoint payload, incl. the geofence fields when requested.

    ``bbox`` is ``(south, west, north, east)`` in decimal degrees.
    """
    w = wire_pb2.Waypoint()
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
    if geofence_radius:
        w.geofence_radius = int(geofence_radius)
    if bbox:
        w.bounding_box.ParseFromString(bounding_box(*bbox))
    flags = wire_pb2.Waypoint
    w.notify_flags = (
        (flags.NOTIFY_ON_ENTER if notify_on_enter else 0)
        | (flags.NOTIFY_ON_EXIT if notify_on_exit else 0)
        | (flags.NOTIFY_FAVORITES_ONLY if notify_favorites_only else 0)
    )
    return w.SerializeToString()


def position_payload(
    lat: float,
    lon: float,
    *,
    altitude: int = 0,
    when: int = 0,
    sats: int = 9,
    precision_bits: int = 32,
) -> bytes:
    p = wire_pb2.Position()
    p.latitude = li(lat)  # the client-link form: full precision, whatever precision_bits says
    p.longitude = li(lon)
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
    hw_model: str | int = "",
    role: str = "CLIENT",
) -> bytes:
    """A User payload. 3.0 carries no id: ``node_id`` only seeds the default names."""
    u = wire_pb2.User()
    u.long_name = long_name or node_id
    u.short_name = short_name or node_id[-4:]
    u.hw_model = hw_model_value(hw_model)
    u.role = _enum(common_pb2.Role, role)  # type: ignore[assignment]
    return u.SerializeToString()


def beacon_payload(
    message: str,
    *,
    offer_channel_name: str = "",
    offer_channel_psk: bytes = b"",
    offer_region: str = "",
    offer_preset: str = "",
) -> bytes:
    """A MeshBeacon payload (MESH_BEACON_APP).

    ``offer_channel_name`` / ``offer_channel_psk`` populate ``offer_channel``.
    ``offer_region`` is a ``RegionCode`` name (``"US"`` or ``"REGION_US"``),
    ``offer_preset`` a ``ModemPreset`` name (``"LONG_FAST"`` or ``"MODEM_LONG_FAST"``).
    """
    b = mesh_beacon_pb2.MeshBeacon()
    if message:
        b.message = message
    if offer_channel_name:
        b.offer_channel.name = offer_channel_name
    if offer_channel_psk:
        b.offer_channel.psk = offer_channel_psk
    if offer_region:
        b.offer_region = region_code(offer_region)  # type: ignore[assignment]
    if offer_preset:
        b.offer_preset = modem_preset(offer_preset)  # type: ignore[assignment]
    return b.SerializeToString()


def device_metrics_payload(
    *, battery_level: int = 101, uptime_s: int = 3600, when: int = 0
) -> bytes:
    """A device-metrics Telemetry payload: what a traceroute reply carries in 3.0."""
    tm = telemetry_pb2.Telemetry()
    tm.time = when or int(time.time())
    tm.device_metrics.battery_level = battery_level
    tm.device_metrics.uptime_minutes = uptime_s // 60  # 3.0 sends uptime in minutes
    return tm.SerializeToString()


def relay_suffix(node_num: int) -> int:
    """The one byte a relay is named by in ``relay_node``, ``next_hop`` and the path tail:
    the NodeNum's last byte, or 0x01 when that byte is 0x00."""
    return node_num & 0xFF or 0x01


def record_path(mp: packet_pb2.MeshPacket, relays: list[int], *, hop_start: int = 7) -> None:
    """Mark ``mp`` as having recorded its path through ``relays`` (NodeNums, oldest
    first): the 3.0 path tail, which is the route record a traceroute reads. The last
    relay is ``relay_node``; the ones before it are the tail."""
    hops = len(relays)
    mp.flags |= packet_pb2.MeshPacket.PACKET_RECORD_PATH
    mp.hop_start = max(hop_start, hops)
    mp.hop_limit = mp.hop_start - hops
    mp.path = bytes(relay_suffix(n) for n in relays[:-1])
    mp.relay_node = relay_suffix(relays[-1] if relays else getattr(mp, "from"))


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
    request_id: int = 0,
) -> packet_pb2.MeshPacket:
    mp = packet_pb2.MeshPacket()
    setattr(mp, "from", from_node & 0xFFFFFFFF)
    mp.to = to_node & 0xFFFFFFFF
    mp.id = _next_id()
    mp.rx_time = rx_time if rx_time is not None else int(time.time())
    mp.hop_limit = hop_limit
    mp.hop_start = max(hop_limit, 3)
    mp.channel = channel_idx
    if want_ack:
        mp.flags |= packet_pb2.MeshPacket.PACKET_WANT_ACK
    mp.decoded.portnum = portnum  # type: ignore[assignment]
    mp.decoded.payload = payload
    # nonzero marks this packet a *response* to that request id — apps gate on
    # it (e.g. a traceroute reply is the response that carries the recorded path).
    if request_id:
        mp.decoded.request_id = request_id & 0xFFFFFFFF
    return mp


# portnum constants used by the builders / inject tool
PORTNUM = {
    "text": PortNum.TEXT_MESSAGE_APP,
    "position": PortNum.POSITION_APP,
    "nodeinfo": PortNum.NODEINFO_APP,
    "waypoint": PortNum.WAYPOINT_APP,
    "beacon": PortNum.MESH_BEACON_APP,
    "traceroute": PortNum.TELEMETRY_APP,  # 3.0: a reply that recorded its path
}


def from_kind(
    kind: str,
    args: dict[str, Any],
    *,
    from_node: int,
    to_node: int = BROADCAST,
    channel_idx: int = 0,
) -> packet_pb2.MeshPacket:
    """Build a MeshPacket from a high-level ``kind`` + ``args`` (the inject API).

    kinds: ``waypoint`` (lat, lon, name, geofence_radius, bbox, notify_on_enter,
    notify_on_exit, notify_favorites_only, icon), ``position`` (lat, lon),
    ``text`` (body; pass reply_id + emoji=true for a tapback — an emoji
    reaction on that message id), ``nodeinfo`` (id, long_name, short_name,
    hw_model, role),
    ``beacon`` (message, offer_channel_name, offer_channel_psk_hex,
    offer_region, offer_preset), ``traceroute`` (route: [node_num, …], the relays
    the reply passed through, oldest first; request_id — the request it answers.
    3.0 has no traceroute message: this is a device-metrics reply that recorded
    its path), ``raw`` (portnum, payload_hex).
    """
    a = args or {}
    if kind == "waypoint":
        pl = waypoint_payload(
            a["lat"],
            a["lon"],
            name=a.get("name", ""),
            description=a.get("description", ""),
            icon=a.get("icon", 0),
            geofence_radius=a.get("geofence_radius", 0),
            bbox=a.get("bbox"),
            notify_on_enter=a.get("notify_on_enter", False),
            notify_on_exit=a.get("notify_on_exit", False),
            notify_favorites_only=a.get("notify_favorites_only", False),
        )
        return packet(
            PortNum.WAYPOINT_APP, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx
        )
    if kind == "position":
        pl = position_payload(a["lat"], a["lon"], altitude=a.get("altitude", 0))
        return packet(
            PortNum.POSITION_APP, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx
        )
    if kind == "text":
        mp = packet(
            PortNum.TEXT_MESSAGE_APP,
            str(a.get("body", "")).encode("utf-8"),
            from_node=from_node,
            to_node=to_node,
            channel_idx=channel_idx,
        )
        # tapback (emoji reaction): body is the emoji, reply_id targets the
        # reacted-to message's packet id, and the emoji flag marks it a reaction
        # rather than a normal reply.
        if a.get("reply_id"):
            mp.decoded.reply_id = int(a["reply_id"]) & 0xFFFFFFFF
        if a.get("emoji"):
            mp.decoded.emoji = 1
        return mp
    if kind == "nodeinfo":
        pl = nodeinfo_payload(
            a.get("id", f"!{from_node:08x}"),
            long_name=a.get("long_name", ""),
            short_name=a.get("short_name", ""),
            hw_model=a.get("hw_model", ""),
            role=a.get("role", "CLIENT"),
        )
        return packet(
            PortNum.NODEINFO_APP, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx
        )
    if kind == "beacon":
        psk_hex = a.get("offer_channel_psk_hex", "")
        pl = beacon_payload(
            a.get("message", ""),
            offer_channel_name=a.get("offer_channel_name", ""),
            offer_channel_psk=bytes.fromhex(psk_hex) if psk_hex else b"",
            offer_region=a.get("offer_region", ""),
            offer_preset=a.get("offer_preset", ""),
        )
        return packet(
            MESH_BEACON_APP, pl, from_node=from_node, to_node=to_node, channel_idx=channel_idx
        )
    if kind == "traceroute":
        mp = packet(
            PortNum.TELEMETRY_APP,
            device_metrics_payload(),
            from_node=from_node,
            to_node=to_node,
            channel_idx=channel_idx,
            request_id=int(a.get("request_id", 0)),
        )
        record_path(mp, [int(n) for n in a.get("route", [])])
        return mp
    if kind == "raw":
        return packet(
            int(a["portnum"]),
            bytes.fromhex(a.get("payload_hex", "")),
            from_node=from_node,
            to_node=to_node,
            channel_idx=channel_idx,
        )
    raise ValueError(f"unknown inject kind: {kind!r}")


def fromradio_from_kind(kind: str, args: dict[str, Any]) -> api_pb2.FromRadio:
    """Build a raw top-level FromRadio message from a high-level ``kind`` + ``args``.

    Counterpart to `from_kind()` for the handshake-only oneofs that have no MeshPacket
    envelope (nothing to route/channel/from-node -- these aren't mesh traffic). Pair with
    `ReplaySession.inject_fromradio()` / `ReplayManager.inject_fromradio()`.

    kinds: ``fileinfo`` (file_name, size_bytes) -- exercises a client's file-manifest
    handler (STATE_SEND_FILEMANIFEST) outside the initial handshake window, e.g. to fuzz-
    test unbounded accumulation or malformed entries under a long-running session.
    ``client_notification`` (variant, message, level, + per-variant fields) -- the device→
    client notifications real firmware pushes (low-entropy/regenerated key, duplicated
    public key, the key-verification handshake), so an app's notification UI can be driven
    hardware-free.
    """
    a = args or {}
    if kind == "fileinfo":
        fr = api_pb2.FromRadio()
        fr.file_info.file_name = a.get("file_name", "")
        # size_bytes is a uint32 on the wire; protobuf rejects a raw negative int.
        # Mask into the unsigned representation so adversarial/negative fuzz values
        # (advertised by replay_inject_fileinfo) still encode instead of raising.
        fr.file_info.size_bytes = int(a.get("size_bytes", 0)) & 0xFFFFFFFF
        return fr
    if kind == "client_notification":
        return _client_notification(a)
    raise ValueError(f"unknown fromradio inject kind: {kind!r}")


# ClientNotification payload_variant oneof names the app renders specially; plus a
# plain-text notification (no variant, just message + level).
CLIENT_NOTIFICATION_VARIANTS = (
    "low_entropy_key",
    "duplicated_public_key",
    "key_verification_number_request",
    "key_verification_number_inform",
    "key_verification_final",
)
# Canned text real firmware attaches to the marker variants (apps also render their own).
_CLIENT_NOTIFICATION_DEFAULT_MSG = {
    "low_entropy_key": "Compromised keys were detected and regenerated.",
    "duplicated_public_key": "A node is advertising a public key that duplicates another node's.",
}


def _client_notification(a: dict[str, Any]) -> api_pb2.FromRadio:
    """Build a FromRadio carrying a ClientNotification (device→client alert)."""
    fr = api_pb2.FromRadio()
    cn = fr.client_notification
    level = str(a.get("level", "WARNING")).upper()
    try:
        cn.level = api_pb2.LogRecord.Level.Value(level)
    except (ValueError, KeyError):
        cn.level = api_pb2.LogRecord.Level.WARNING
    variant = a.get("variant")
    message = a.get("message") or _CLIENT_NOTIFICATION_DEFAULT_MSG.get(variant or "", "")
    if message:
        cn.message = str(message)
    if a.get("reply_id"):
        cn.reply_id = int(a["reply_id"]) & 0xFFFFFFFF
    if a.get("time"):
        cn.time = int(a["time"]) & 0xFFFFFFFF
    if variant in (None, "", "text"):
        return fr  # plain text notification, no oneof variant
    if variant == "low_entropy_key":
        cn.low_entropy_key.SetInParent()
    elif variant == "duplicated_public_key":
        cn.duplicated_public_key.SetInParent()
    elif variant == "key_verification_number_request":
        kv = cn.key_verification_number_request
        kv.nonce = int(a.get("nonce", 0)) & 0xFFFFFFFFFFFFFFFF
        kv.remote_longname = str(a.get("remote_longname", ""))
    elif variant == "key_verification_number_inform":
        kv = cn.key_verification_number_inform
        kv.nonce = int(a.get("nonce", 0)) & 0xFFFFFFFFFFFFFFFF
        kv.remote_longname = str(a.get("remote_longname", ""))
        kv.security_number = int(a.get("security_number", 0)) & 0xFFFFFFFF
    elif variant == "key_verification_final":
        kv = cn.key_verification_final
        kv.nonce = int(a.get("nonce", 0)) & 0xFFFFFFFFFFFFFFFF
        kv.remote_longname = str(a.get("remote_longname", ""))
        kv.isSender = bool(a.get("is_sender", False))
        kv.verification_characters = str(a.get("verification_characters", ""))
    else:
        raise ValueError(
            f"unknown client_notification variant {variant!r}; "
            f"expected one of {(*CLIENT_NOTIFICATION_VARIANTS, 'text')}"
        )
    return fr
