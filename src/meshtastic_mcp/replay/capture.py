# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture sources for the replay engine.

A *capture* is the data a replay session streams: a node DB, a channel list,
and a time-ordered sequence of packets. Three sources are supported, all
normalised to the same :class:`Capture` shape so the engine is source-agnostic:

  1. **SQLite** (`from_sqlite`) — full fidelity. Reads the schema shared by the
     Burning Man / DEF CON / MeshCon captures (`node`, `packet`, `packet_seen`),
     where ``packet.payload`` is an already-decoded ``MeshPacket`` protobuf blob.
     This is the canonical path; DEF CON datasets drop straight in here.
  2. **Recorder JSONL** (`from_recorder_jsonl`) — best-effort. Reconstructs
     minimal ``MeshPacket``s from the recorder's *summaries* (`packets.jsonl`).
     Payloads beyond the recorded 64-byte hex prefix are lost, so this is good
     for timing/volume realism but not byte-exact replay.
  3. **In-memory** (`Capture` built directly) — used by ``sim.py`` to stream a
     generated MeshCon mesh without a DB round-trip.

Packets are normalised to ``(rx_time:int, meshpacket_bytes:bytes,
channel_name:str)``; the engine restamps ``rx_time`` to "now" and maps the
channel name to an index at stream time.

For captures with multiple real channels, pass a caller-supplied ``channel_specs``
list to :func:`from_sqlite`: packets are routed into the named channels by their
OTA channel hash and the engine advertises the channels' PSKs, so a connecting
app shows the true channels and live-decrypts encrypted packets. The channel set
is plain data (name + PSK, optional explicit hashes / catch-all), so this stays
agnostic to any particular event.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meshtastic.protobuf import config_pb2, mesh_pb2, portnums_pb2

# MQTT topic labels that aren't real channels (PKI = public-key direct messages).
_NON_CHANNEL_TOPICS = {"PKI"}


@dataclass
class NodeRow:
    """A node-DB entry, decoupled from any protobuf shape."""

    num: int
    node_id: str
    long_name: str | None = None
    short_name: str | None = None
    hw_model: str | None = None
    role: str | None = None
    lat_i: int | None = None
    lon_i: int | None = None


@dataclass
class ChannelSpec:
    """A channel the engine advertises: name, PSK, primary flag, OTA hashes.

    ``psk`` is the real key, so an app given these specs live-decrypts the
    still-encrypted packets the engine streams. A capture packet carries its true
    channel hash in ``MeshPacket.channel``; routing matches that against this
    channel's hashes — ``ota_hashes`` if given, otherwise the hash derived from
    ``(name, psk)``. Set ``catch_all`` on one channel to receive packets whose
    hash matches nothing (e.g. an "Encrypted"/"Unknown" bucket).
    """

    name: str
    psk: bytes = b"\x01"  # well-known default key
    primary: bool = False
    ota_hashes: tuple[int, ...] = ()
    catch_all: bool = False

    @property
    def app_name(self) -> str:
        # a blank primary name => the app renders the modem-preset default label
        return "" if self.name == "LongFast" else self.name

    def hashes(self) -> tuple[int, ...]:
        """OTA hashes this channel claims (explicit, or derived from name+psk)."""
        return self.ota_hashes or (channel_hash(self.name, self.psk),)


@dataclass
class Capture:
    """Normalised replay payload: nodes + channels + time-ordered packets."""

    nodes: list[NodeRow] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    # (rx_time_epoch, serialized MeshPacket bytes, channel_name)
    packets: list[tuple[int, bytes, str]] = field(default_factory=list)
    label: str = "capture"
    # Optional real channel keys/roles. When set, the engine advertises these
    # (name + PSK + primary) so the app decrypts encrypted packets in-stream;
    # when None it advertises placeholder PSKs keyed off the channel names.
    channel_specs: list[ChannelSpec] | None = None

    def window(self, start: int | None, end: int | None) -> list[tuple[int, bytes, str]]:
        """Packets whose rx_time falls in [start, end) (open bounds when None)."""
        out = self.packets
        if start:
            out = [p for p in out if p[0] >= start]
        if end:
            out = [p for p in out if p[0] < end]
        return out

    @property
    def span(self) -> tuple[int, int]:
        if not self.packets:
            return (0, 0)
        return (self.packets[0][0], self.packets[-1][0])

    def center(self) -> tuple[int, int] | None:
        """Median (lat_i, lon_i) over positioned nodes — a sensible "you are here"
        for the connected node so the app's map/distances look right."""
        import statistics

        lats = [n.lat_i for n in self.nodes if n.lat_i]
        lons = [n.lon_i for n in self.nodes if n.lon_i]
        if not lats or not lons:
            return None
        return (int(statistics.median(lats)), int(statistics.median(lons)))


# Meshtastic default PSK: a 1-byte key 0x01..0x0A selects this with the last byte
# varied; 16/32-byte keys are used as-is.
_DEFAULT_KEY = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59, 0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)


def expand_psk(raw: bytes) -> bytes:
    """Expand a Meshtastic PSK (1-byte default-key selector, or 16/32-byte key)."""
    if len(raw) == 1:
        n = raw[0]
        if n == 0:
            return b""
        key = bytearray(_DEFAULT_KEY)
        key[-1] = (_DEFAULT_KEY[-1] + (n - 1)) & 0xFF
        return bytes(key)
    return raw


def _xor_fold(data: bytes) -> int:
    h = 0
    for b in data:
        h ^= b
    return h


def channel_hash(name: str, psk: bytes) -> int:
    """Meshtastic OTA channel hash = xorHash(name) ^ xorHash(expanded psk)."""
    return _xor_fold(name.encode()) ^ _xor_fold(expand_psk(psk))


def _spec_from_dict(d: dict[str, Any]) -> ChannelSpec:
    """Build a ChannelSpec from a plain dict (psk as base64 str or raw bytes)."""
    psk = d.get("psk", b"\x01")
    if isinstance(psk, str):
        import base64

        psk = base64.b64decode(psk)
    return ChannelSpec(
        name=str(d["name"]),
        psk=bytes(psk),
        primary=bool(d.get("primary", False)),
        ota_hashes=tuple(d.get("ota_hashes", ()) or ()),
        catch_all=bool(d.get("catch_all", False)),
    )


def resolve_channel_specs(
    spec: list[ChannelSpec] | list[dict[str, Any]] | None,
) -> list[ChannelSpec] | None:
    """Normalise caller-supplied channel specs (ChannelSpec or dicts) to a list.

    The caller owns the channel set — names, PSKs, and (optionally) OTA hashes —
    so this stays agnostic to any particular event. Pass dicts like
    ``{"name": "…", "psk": "<base64>", "primary": true, "catch_all": true}``.
    """
    if not spec:
        return None
    return [s if isinstance(s, ChannelSpec) else _spec_from_dict(s) for s in spec]


def _enum(enum_type: Any, name: str | None, default: int = 0) -> int:
    if not name:
        return default
    try:
        return enum_type.Value(str(name).strip().upper())
    except (ValueError, KeyError):
        return default


def _resolve_db(path: str | os.PathLike[str]) -> str:
    """Decompress ``*.gz`` captures to a temp file; pass ``.db`` through."""
    p = Path(path).expanduser()
    if p.suffix == ".gz":
        cache = Path(tempfile.gettempdir()) / "meshtastic-mcp-replay"
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / p.name[:-3]
        if not out.exists() or out.stat().st_mtime < p.stat().st_mtime:
            with gzip.open(p, "rb") as fi, open(out, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        return str(out)
    return str(p)


def from_sqlite(
    path: str | os.PathLike[str],
    *,
    limit_nodes: int = 200,
    label: str | None = None,
    channel_specs: list[ChannelSpec] | list[dict[str, Any]] | None = None,
) -> Capture:
    """Load a capture from a BM/DEF CON/MeshCon-schema SQLite DB (or ``.gz``).

    When ``channel_specs`` is given (a caller-supplied list of channels, each with
    a name + PSK and optionally explicit OTA hashes), packets are routed into
    those channels by their OTA channel hash (``MeshPacket.channel``) and the
    engine advertises the real PSKs so the app live-decrypts encrypted packets.
    Otherwise channels come from the capture's ``packet.channel`` name column
    with placeholder keys.
    """
    specs = resolve_channel_specs(channel_specs)
    db = _resolve_db(path)
    conn = sqlite3.connect(db)
    try:
        cap = Capture(label=label or Path(str(path)).stem)

        if specs is not None:
            cap.channel_specs = specs
            cap.channels = [s.name for s in specs]
        else:
            # Channels: distinct, busiest first, LongFast forced to the primary slot.
            rows = conn.execute(
                "SELECT channel, COUNT(*) c FROM packet "
                "WHERE channel IS NOT NULL AND channel != '' "
                "GROUP BY channel ORDER BY c DESC"
            ).fetchall()
            names = [r[0] for r in rows if r[0] not in _NON_CHANNEL_TOPICS]
            if "LongFast" in names:
                names.remove("LongFast")
                names = ["LongFast", *names]
            cap.channels = names[:8] or ["LongFast"]

        # Nodes: most-recently-active first (like a real device's node DB).
        sql = (
            "SELECT id, node_id, long_name, short_name, hw_model, role, "
            "last_lat, last_long FROM node WHERE node_id IS NOT NULL "
            "ORDER BY last_update DESC"
        )
        if limit_nodes:
            sql += f" LIMIT {int(limit_nodes)}"
        for nid, num, ln, sn, hw, role, lat, lon in conn.execute(sql):
            cap.nodes.append(
                NodeRow(
                    num=int(num) & 0xFFFFFFFF,
                    node_id=nid or f"!{int(num) & 0xFFFFFFFF:08x}",
                    long_name=ln,
                    short_name=sn,
                    hw_model=hw,
                    role=role,
                    lat_i=int(lat) if lat else None,
                    lon_i=int(lon) if lon else None,
                )
            )

        # Packets: one row per packet, ordered by earliest reception.
        cur = conn.execute(
            "SELECT p.payload, p.channel, MIN(ps.rx_time) AS rxt "
            "FROM packet p JOIN packet_seen ps ON ps.packet_id = p.id "
            "WHERE ps.rx_time > 0 GROUP BY p.id ORDER BY rxt ASC"
        )
        if specs is not None:
            # route each packet by its OTA channel hash into a named channel
            hash_to_name = {h: s.name for s in specs for h in s.hashes()}
            catch = next((s for s in specs if s.catch_all), specs[-1])
            fallback = catch.name
            mp = mesh_pb2.MeshPacket()
            packets = []
            for payload, _ch, rxt in cur:
                if not payload:
                    continue
                mp.Clear()
                try:
                    mp.ParseFromString(bytes(payload))
                except Exception:
                    continue
                packets.append((int(rxt), bytes(payload), hash_to_name.get(mp.channel, fallback)))
            cap.packets = packets
        else:
            cap.packets = [
                (int(rxt), bytes(payload), ch or "LongFast") for payload, ch, rxt in cur if payload
            ]
        return cap
    finally:
        conn.close()


def from_events(
    events: list[dict[str, Any]], *, start: int | None = None, label: str = "scenario"
) -> Capture:
    """Build a scripted capture from a list of events (for app-feature tests).

    Each event: ``{kind, args, from, to?, channel?, delay?}`` — ``kind`` and
    ``args`` are the same as ``replay_inject`` / :mod:`build` (waypoint, position,
    text, nodeinfo, raw); ``delay`` is seconds after the previous event (default
    1). Nodes referenced by NodeInfo events populate the node DB. Mirrors
    :func:`from_sqlite` as a capture source, so a scenario can be replayed,
    windowed, fuzzed, or paced like any other.
    """
    import time as _t

    from . import build

    t = int(start if start is not None else _t.time())
    cap = Capture(label=label, channels=["LongFast"])
    seen: dict[int, NodeRow] = {}
    for ev in events:
        t += int(ev.get("delay", 1))
        frm = int(ev["from"])
        mp = build.from_kind(
            ev["kind"],
            ev.get("args", {}),
            from_node=frm,
            to_node=int(ev.get("to", 0xFFFFFFFF)),
        )
        mp.rx_time = t
        ch = str(ev.get("channel", "LongFast"))
        if ch not in cap.channels:
            cap.channels.append(ch)
        cap.packets.append((t, mp.SerializeToString(), ch))
        a = ev.get("args", {})
        if ev["kind"] == "nodeinfo":
            seen[frm] = NodeRow(
                frm,
                a.get("id", f"!{frm:08x}"),
                a.get("long_name"),
                a.get("short_name"),
                a.get("hw_model"),
                a.get("role"),
            )
        elif frm not in seen:
            seen[frm] = NodeRow(frm, f"!{frm:08x}")
    cap.nodes = list(seen.values())
    return cap


def from_recorder_jsonl(path: str | os.PathLike[str], *, label: str | None = None) -> Capture:
    """Best-effort capture from a recorder ``packets.jsonl`` summary stream.

    The recorder stores summaries (portnum, from/to, hop, a 64-byte payload hex
    prefix), not full packets, so this reconstructs *minimal* MeshPackets. Use
    for timing/volume realism, not byte-exact payloads.
    """
    cap = Capture(label=label or Path(str(path)).stem)
    channels: set[str] = set()
    nodes: dict[int, NodeRow] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pkt = row.get("packet") if isinstance(row.get("packet"), dict) else row
            ts = row.get("ts") or row.get("rx_time")
            if ts is None:
                continue
            mp = mesh_pb2.MeshPacket()
            frm = _coerce_num(pkt.get("from_node") or pkt.get("from"))
            to = _coerce_num(pkt.get("to_node") or pkt.get("to"), default=0xFFFFFFFF) or 0xFFFFFFFF
            if frm is not None:
                setattr(mp, "from", frm & 0xFFFFFFFF)
                nodes.setdefault(
                    frm & 0xFFFFFFFF, NodeRow(frm & 0xFFFFFFFF, f"!{frm & 0xFFFFFFFF:08x}")
                )
            mp.to = to & 0xFFFFFFFF
            pn = pkt.get("portnum")
            if isinstance(pn, str):
                pn = _enum(portnums_pb2.PortNum, pn)
            if pn:
                mp.decoded.portnum = pn
            hx = pkt.get("payload_hex_prefix")
            if hx:
                try:
                    mp.decoded.payload = bytes.fromhex(hx)
                except ValueError:
                    pass
            if pkt.get("hop_limit") is not None:
                mp.hop_limit = int(pkt["hop_limit"])
            ch = str(pkt.get("channel") or "LongFast")
            channels.add(ch)
            cap.packets.append((int(ts), mp.SerializeToString(), ch))
    cap.packets.sort(key=lambda p: p[0])
    cap.nodes = list(nodes.values())
    cap.channels = (["LongFast"] if "LongFast" in channels else []) + sorted(
        c for c in channels if c != "LongFast"
    )
    cap.channels = cap.channels or ["LongFast"]
    return cap


def _coerce_num(v: Any, default: int | None = None) -> int | None:
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip().lstrip("!")
    try:
        return int(s, 16) if all(c in "0123456789abcdefABCDEF" for c in s) else int(s)
    except ValueError:
        return default


# ── Protobuf builders the engine reuses (kept here so capture owns the schema) ─
def node_to_nodeinfo(n: NodeRow, *, last_heard: int) -> mesh_pb2.NodeInfo:
    ni = mesh_pb2.NodeInfo()
    ni.num = n.num & 0xFFFFFFFF
    ni.user.id = n.node_id or f"!{ni.num:08x}"
    ni.user.long_name = n.long_name or ni.user.id
    ni.user.short_name = n.short_name or ni.user.id[-4:]
    ni.user.hw_model = _enum(mesh_pb2.HardwareModel, n.hw_model)
    ni.user.role = _enum(config_pb2.Config.DeviceConfig.Role, n.role)
    if n.lat_i and n.lon_i:
        ni.position.latitude_i = n.lat_i
        ni.position.longitude_i = n.lon_i
    ni.last_heard = last_heard
    ni.hops_away = 1
    ni.snr = 6.0
    return ni
