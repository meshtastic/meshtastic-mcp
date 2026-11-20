# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Stable names for the radios on this host, and what each one was last seen as.

A radio is named the first time a connection to it succeeds, `<model>-<last four hex of its
node id>` (`heltec-v3-aa13`), and keeps that name: a reboot, a new `/dev/tty*` or a trip
through UF2 does not rename it. The record is keyed like a lease (USB VID:serial or the TCP
endpoint) and kept in `<data dir>/devices.json`, so every server on the host shares it.
Nothing here opens a port; the node fields are whatever the last connection saw.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from . import leases


def _file() -> Path:
    from .config import mcp_data_dir

    return mcp_data_dir() / "devices.json"


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data.get("devices", {}) if isinstance(data, dict) else {}


def _save(records: dict[str, dict[str, Any]]) -> None:
    path = _file()
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"devices": records}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _lock():
    return leases.file_lock(_file().with_suffix(".lock"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _auto_name(model: str | None, node_num: int | None, taken: set[str]) -> str | None:
    if not model or node_num is None:
        return None
    base = f"{_slug(model)}-{node_num & 0xFFFF:04x}"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def record_seen(port: str, node: dict[str, Any]) -> dict[str, Any]:
    """Store what a successful connection to `port` saw, naming the radio if it has no name.

    `node` holds `node_num`, `model`, `long_name` and `short_name` (any may be None).
    """
    key = leases.device_key(port)
    with _lock():
        records = _load()
        rec = records.get(key, {})
        taken = {r["name"] for k, r in records.items() if k != key and r.get("name")}
        rec.update({k: v for k, v in node.items() if v is not None})
        rec["port"] = port
        rec["seen_at"] = time.time()
        if not rec.get("name"):
            rec["name"] = _auto_name(rec.get("model"), rec.get("node_num"), taken)
        records[key] = rec
        _save(records)
        return {"key": key, **rec}


def rename(port_or_name: str, name: str) -> dict[str, Any]:
    """Give a radio a name of your choosing. Raises `ValueError` if another radio has it."""
    name = _slug(name)
    if not name:
        raise ValueError("a name needs at least one letter or digit")
    with _lock():
        records = _load()
        key = _key_of(port_or_name, records)
        if any(r.get("name") == name for k, r in records.items() if k != key):
            raise ValueError(f"another radio is already named {name}")
        rec = records.get(key, {"port": port_or_name})
        rec["name"] = name
        records[key] = rec
        _save(records)
        return {"key": key, **rec}


def _key_of(port_or_name: str, records: dict[str, dict[str, Any]]) -> str:
    bare = _strip_host(port_or_name)
    for key, rec in records.items():
        if rec.get("name") == bare:
            return key
    return leases.device_key(port_or_name)


def _strip_host(name: str) -> str:
    host, sep, rest = name.partition("/")
    return rest if sep and host == socket.gethostname() else name


def looks_like_name(value: str) -> bool:
    """A name is a bare slug, or `<this host>/<slug>`; paths and `tcp://` are ports."""
    bare = _strip_host(value)
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", bare)) and not re.fullmatch(r"(?i)com\d+", bare)


def resolve(name: str, attached: list[str]) -> str | None:
    """The attached port whose radio is called `name`, None if no radio has that name.

    Raises `LookupError` when the radio is known but not attached now.
    """
    bare = _strip_host(name)
    records = _load()
    key = next((k for k, r in records.items() if r.get("name") == bare), None)
    if key is None:
        return None
    for port in attached:
        if leases.device_key(port) == key:
            return port
    if key.startswith("tcp:"):
        return "tcp://" + key[len("tcp:") :]
    raise LookupError(f"{bare} is not attached to {socket.gethostname()} right now")


def listing(attached: list[str]) -> list[dict[str, Any]]:
    """Every attached radio and every radio seen before, with its name and last-seen fields."""
    records = _load()
    host = socket.gethostname()
    rows: dict[str, dict[str, Any]] = {}
    for port in attached:
        key = leases.device_key(port)
        rows[key] = {**records.get(key, {}), "key": key, "port": port, "attached": True}
    for key, rec in records.items():
        if key not in rows:
            rows[key] = {**rec, "key": key, "attached": False}
    leased = {r["device"]: r for r in leases.active()}
    out = []
    for key, row in rows.items():
        name = row.get("name")
        lease = leased.get(key)
        out.append(
            {
                "name": f"{host}/{name}" if name else None,
                "port": row.get("port"),
                "attached": row["attached"],
                "node_num": row.get("node_num"),
                "model": row.get("model"),
                "long_name": row.get("long_name"),
                "short_name": row.get("short_name"),
                "last_seen": row.get("seen_at"),
                "leased_to": (lease.get("holder") or "another session") if lease else None,
                "key": key,
            }
        )
    return sorted(out, key=lambda r: (not r["attached"], r["name"] or "~", r["port"] or ""))
