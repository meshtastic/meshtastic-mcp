#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Convert DEF CON 33-style gateway text logs into the shared capture SQLite schema.

The darknet-ng DEF CON 33 dataset is ``str(dict)`` dumps from the meshtastic
Python library — one received packet per record, each carrying a top-level
``'raw': <text-format MeshPacket>`` block. This tool parses that MeshPacket,
stores it as ``packet.payload`` (a full serialized MeshPacket — the same shape
as the Burning Man capture), records every individual reception in
``packet_seen`` (duplicates = rebroadcast copies the gateway heard), and builds
the ``node`` table from NODEINFO/POSITION packets. The result loads with
``meshtastic_mcp.replay.capture.from_sqlite`` and scores with
``meshtastic_mcp.replay.metrics``.

Usage:
    python tools/import_defcon_logs.py --out defcon33.db <log.txt> [<log2.txt> ...]

Channel name per record is inferred from the filename's modem preset
(``LongFast`` / ``ShortTurbo``); override with ``--channel``.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import zlib
from collections.abc import Iterator
from pathlib import Path

from google.protobuf import text_format
from meshtastic.protobuf import config_pb2, mesh_pb2

SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
    id VARCHAR NOT NULL, node_id BIGINT, long_name VARCHAR, short_name VARCHAR,
    hw_model VARCHAR, firmware VARCHAR, role VARCHAR, last_lat BIGINT,
    last_long BIGINT, channel VARCHAR, last_update DATETIME,
    PRIMARY KEY (id), UNIQUE (node_id));
CREATE TABLE IF NOT EXISTS packet (
    id BIGINT NOT NULL, portnum INTEGER, from_node_id BIGINT, to_node_id BIGINT,
    payload BLOB, import_time DATETIME, channel VARCHAR, PRIMARY KEY (id));
CREATE TABLE IF NOT EXISTS packet_seen (
    packet_id BIGINT NOT NULL, node_id BIGINT NOT NULL, rx_time BIGINT NOT NULL,
    hop_limit INTEGER, hop_start INTEGER, channel VARCHAR, rx_snr FLOAT,
    rx_rssi INTEGER, topic VARCHAR, import_time DATETIME,
    PRIMARY KEY (packet_id, node_id, rx_time));
"""

# The top-level MeshPacket text block always starts at `from:`; nested `raw:`
# blocks (telemetry starts at `time:`, User starts at `id:`) never do.
RAW_RE = re.compile(r"'raw': (from:.*?)(?=, 'fromId'|, 'toId'|\}\s*$)", re.S)


def _records(path: Path) -> Iterator[str]:
    """Yield one str(dict) record at a time (records start with ``{'from'``)."""
    rec: list[str] = []
    with path.open(errors="replace") as fh:
        for line in fh:
            if line.startswith("{'from'") and rec:
                yield "".join(rec)
                rec = [line]
            else:
                rec.append(line)
    if rec:
        yield "".join(rec)


def _channel_for(path: Path, override: str | None) -> str:
    if override:
        return override
    name = path.name.lower()
    if "shortturbo" in name or "short_turbo" in name:
        return "ShortTurbo"
    return "LongFast"


def _gateway_id(path: Path) -> int:
    """Stable synthetic node id per source file (0xDF-prefixed, non-colliding)."""
    return 0xDF000000 | (zlib.crc32(path.name.encode()) & 0x00FFFFFF)


def _parse_packet(record: str) -> mesh_pb2.MeshPacket | None:
    m = RAW_RE.search(record)
    if not m:
        return None
    mp = mesh_pb2.MeshPacket()
    try:
        text_format.Parse(m.group(1), mp, allow_unknown_field=True)
    except text_format.ParseError:
        return None
    return mp


def _enum_name(enum_type: object, value: int) -> str:
    """Enum name, falling back to the numeric value for enums newer than our protos."""
    try:
        return enum_type.Name(value)  # type: ignore[attr-defined]
    except ValueError:
        return str(value)


def _upsert_node(conn: sqlite3.Connection, mp: mesh_pb2.MeshPacket) -> None:
    frm = getattr(mp, "from") & 0xFFFFFFFF
    pn = mp.decoded.portnum
    if pn == 4:  # NODEINFO -> User
        user = mesh_pb2.User()
        try:
            user.ParseFromString(mp.decoded.payload)
        except Exception:
            return
        hw = _enum_name(mesh_pb2.HardwareModel, user.hw_model) if user.hw_model else None
        role = _enum_name(config_pb2.Config.DeviceConfig.Role, user.role) if user.role else "CLIENT"
        conn.execute(
            "INSERT INTO node (id, node_id, long_name, short_name, hw_model, role, last_update)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET"
            " long_name=excluded.long_name, short_name=excluded.short_name,"
            " hw_model=excluded.hw_model, role=excluded.role, last_update=excluded.last_update",
            (
                user.id or f"!{frm:08x}",
                frm,
                user.long_name,
                user.short_name,
                hw,
                role,
                mp.rx_time,
            ),
        )
    elif pn == 3:  # POSITION -> last fix
        pos = mesh_pb2.Position()
        try:
            pos.ParseFromString(mp.decoded.payload)
        except Exception:
            return
        if pos.latitude_i or pos.longitude_i:
            conn.execute(
                "UPDATE node SET last_lat=?, last_long=? WHERE node_id=?",
                (pos.latitude_i, pos.longitude_i, frm),
            )


def import_logs(out_db: str, paths: list[Path], channel: str | None = None) -> dict[str, int]:
    conn = sqlite3.connect(out_db)
    conn.executescript(SCHEMA)
    stats = {"records": 0, "parsed": 0, "packets": 0, "seen": 0}
    try:
        for path in paths:
            ch = _channel_for(path, channel)
            gw = _gateway_id(path)
            for record in _records(path):
                stats["records"] += 1
                mp = _parse_packet(record)
                if mp is None:
                    continue
                stats["parsed"] += 1
                pn = mp.decoded.portnum if mp.WhichOneof("payload_variant") == "decoded" else None
                cur = conn.execute(
                    "INSERT OR IGNORE INTO packet (id, portnum, from_node_id, to_node_id,"
                    " payload, channel) VALUES (?,?,?,?,?,?)",
                    (
                        mp.id,
                        pn,
                        getattr(mp, "from") & 0xFFFFFFFF,
                        mp.to & 0xFFFFFFFF,
                        mp.SerializeToString(),
                        ch,
                    ),
                )
                stats["packets"] += cur.rowcount
                cur = conn.execute(
                    "INSERT OR IGNORE INTO packet_seen (packet_id, node_id, rx_time,"
                    " hop_limit, hop_start, channel, rx_snr, rx_rssi) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        mp.id,
                        gw,
                        mp.rx_time,
                        mp.hop_limit,
                        mp.hop_start,
                        ch,
                        round(mp.rx_snr, 2) if mp.rx_snr else None,
                        mp.rx_rssi or None,
                    ),
                )
                stats["seen"] += cur.rowcount
                if pn in (3, 4):
                    _upsert_node(conn, mp)
            conn.commit()
            print(f"{path.name}: cumulative {stats}", file=sys.stderr)
        return stats
    finally:
        conn.commit()
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="output SQLite path")
    ap.add_argument("--channel", help="force a channel name (default: infer from filename)")
    ap.add_argument("logs", nargs="+", type=Path)
    args = ap.parse_args()
    stats = import_logs(args.out, args.logs, channel=args.channel)
    print(stats)


if __name__ == "__main__":
    main()
