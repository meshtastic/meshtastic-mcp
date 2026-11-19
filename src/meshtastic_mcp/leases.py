# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Device leases shared by every meshtastic-mcp server on a host.

A lease gives one caller a radio for a while. It is a JSON file under
``<data dir>/leases/``, so stdio servers started by different sessions and one ``serve``
process all see the same leases. A device is keyed by its USB identity (VID, PID, serial
number), not its port, because a board's ``/dev/tty*`` changes when it reboots, enters DFU
or is replugged; a TCP node is keyed by its endpoint.

A port with no live lease works as before. While a lease is live, a device tool must pass
that lease's handle or it is refused. Only the TTL ends a lease nobody releases, and only a
human breaks one early (``meshtastic-mcp leases --break``).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_TTL_S = 3600
DEFAULT_TTL_S = 600

# The handle the current tool call passed, set by the server's lease transform and copied
# into job threads by `jobs.start`.
CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("lease", default=None)


class LeaseError(RuntimeError):
    """A claim or a device call conflicts with a lease; the message says who holds it."""


def _dir() -> Path:
    from .config import mcp_data_dir

    d = mcp_data_dir() / "leases"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextlib.contextmanager
def _store_lock() -> Iterator[None]:
    with open(_dir() / ".lock", "a+b") as fh:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def device_key(port: str) -> str:
    """The identity a lease is held on: USB VID:PID:serial, else the resolved path or endpoint."""
    from .connection import is_tcp_port, normalize_tcp_endpoint

    if is_tcp_port(port):
        return "tcp:" + normalize_tcp_endpoint(port)
    real = os.path.realpath(port)
    try:
        from serial.tools import list_ports

        for info in list_ports.comports():
            if os.path.realpath(info.device) == real and info.serial_number:
                return f"usb:{info.vid or 0:04x}:{info.pid or 0:04x}:{info.serial_number}"
    except Exception:
        pass
    return "dev:" + real


def _path(key: str) -> Path:
    return _dir() / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write(path: Path, record: dict[str, Any]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)


def _live(record: dict[str, Any] | None, now: float) -> bool:
    return record is not None and float(record.get("expires_at", 0)) > now


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "lease"}


def claim(
    port: str, *, ttl_s: float = DEFAULT_TTL_S, holder: str | None = None, lease: str | None = None
) -> dict[str, Any]:
    """Take a device, or renew the lease `lease` on it. Raises `LeaseError` if another holds it."""
    if not 0 < ttl_s <= MAX_TTL_S:
        raise LeaseError(f"ttl_s must be between 1 and {MAX_TTL_S}")
    key = device_key(port)
    path = _path(key)
    now = time.time()
    with _store_lock():
        current = _read(path)
        if _live(current, now):
            assert current is not None
            if lease is None or current["lease"] != lease:
                raise LeaseError(_held_message(current, now))
            current["expires_at"] = now + ttl_s
            _write(path, current)
            return current
        if lease is not None:
            raise LeaseError(f"lease {lease} on {port} has expired; claim the device again")
        record = {
            "lease": "lease_" + secrets.token_urlsafe(12),
            "device": key,
            "port": port,
            "holder": holder or "",
            "host": socket.gethostname(),
            "claimed_at": now,
            "expires_at": now + ttl_s,
        }
        _write(path, record)
        return record


def renew(lease: str, ttl_s: float) -> bool:
    """Extend `lease` wherever it is held. False if it is gone or expired."""
    with _store_lock():
        now = time.time()
        for path in _dir().glob("*.json"):
            record = _read(path)
            if record and record.get("lease") == lease and _live(record, now):
                record["expires_at"] = max(record["expires_at"], now + ttl_s)
                _write(path, record)
                return True
    return False


def release(lease: str) -> dict[str, Any]:
    """End `lease`. Raises `LeaseError` if no live lease has that handle."""
    with _store_lock():
        now = time.time()
        for path in _dir().glob("*.json"):
            record = _read(path)
            if record and record.get("lease") == lease:
                path.unlink(missing_ok=True)
                if not _live(record, now):
                    raise LeaseError(f"lease {lease} had already expired")
                return _public(record)
    raise LeaseError(f"no lease {lease}")


def active() -> list[dict[str, Any]]:
    """Every live lease on this host, without its handle."""
    now = time.time()
    out = []
    with _store_lock():
        for path in _dir().glob("*.json"):
            record = _read(path)
            if _live(record, now):
                assert record is not None
                out.append(_public(record))
            elif record is not None:
                path.unlink(missing_ok=True)
    return sorted(out, key=lambda r: r["device"])


def break_lease(port_or_device: str) -> dict[str, Any] | None:
    """Drop the lease on a device, whoever holds it. For a human, never an agent tool."""
    path = _path(port_or_device)
    if not path.exists():
        path = _path(device_key(port_or_device))
    with _store_lock():
        record = _read(path)
        path.unlink(missing_ok=True)
    return _public(record) if record else None


def check(port: str, lease: str | None) -> None:
    """Refuse a device call on `port` unless it is unleased or `lease` holds it."""
    path = _path(device_key(port))
    now = time.time()
    record = _read(path)
    if not _live(record, now):
        if lease is not None:
            raise LeaseError(f"lease {lease} does not hold {port}; claim it first")
        return
    assert record is not None
    if record["lease"] != lease:
        raise LeaseError(_held_message(record, now))


def _held_message(record: dict[str, Any], now: float) -> str:
    who = record.get("holder") or "another session"
    left = int(record["expires_at"] - now)
    return (
        f"{record['port']} is leased to {who} on {record.get('host')} for another {left}s. "
        "Use another device, or wait for the lease to end."
    )
