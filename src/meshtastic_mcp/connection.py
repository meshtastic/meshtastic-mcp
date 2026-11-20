# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Context manager for meshtastic interface connections (serial + TCP).

Every info/admin tool goes through `connect(port)` so we have a single place
that:
  - auto-selects the port when one likely_meshtastic device is present,
  - fails fast if a serial_session is already holding the port,
  - guarantees `.close()` is called, even on exception.

Two transports:
  - Serial: USB-attached firmware on `/dev/cu.*` / `/dev/ttyUSB*` / `COM*`.
  - TCP: a `meshtasticd` daemon (e.g. the native macOS / Linux Portduino
    headless build) addressed as `tcp://host[:port]` (default port 4403).
    Surfaced by `devices.list_devices()` when `MESHTASTIC_MCP_TCP_HOST` is
    set, so `resolve_port(None)` auto-selects it like a USB candidate.

Both `SerialInterface` and `TCPInterface` block on construction waiting for
the node database; that's fine for v1 since every tool is a short-lived
request.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import device_names, devices, registry

log = logging.getLogger("meshtastic_mcp.connection")

DEFAULT_TCP_PORT = 4403
TCP_SCHEME = "tcp://"
BLE_SCHEME = "ble:"
TCP_HOST_ENV = "MESHTASTIC_MCP_TCP_HOST"

# Upper bound on `connect(linger_s=...)`. The linger holds the exclusive port
# lock, so an unbounded caller value would pin the device and fail-fast every
# other operation for as long as it lasts. Even the most generous real need
# (broadcast politeness delay + airtime, or a flash-write commit) is a handful
# of seconds; 60s is comfortably above that and caps the damage from a bad arg.
_MAX_LINGER_S = 60.0

# Upper bound on a graceful close. `iface.close()` sends a disconnect, and if the
# device just rebooted (factory_reset / reboot / shutdown) the meshtastic library
# loops forever waiting for TX-queue space that never frees — a 600s test hang in
# the wild. We close on a daemon thread and abandon it after this long; the OS
# reclaims the fd when the device re-enumerates or this process exits.
_CLOSE_TIMEOUT_S = 5.0


# Extra time past `timeout_s` for opening a serial interface. The library's own timeout bounds
# only the config wait; a wedged nRF52 CDC can block the open itself, and a real `device_info`
# sat there for 19 minutes.
_OPEN_GRACE_S = 10.0

# Ports whose open was abandoned and is still blocked, keyed by resolved port. A call to one of
# these fails fast instead of starting another blocked open.
_wedged_opens: dict[str, threading.Thread] = {}


# A radio that is restarting can miss the client's one `want_config`, and the library then waits
# 30 s for a config that never comes, whatever timeout it was given. Resend it while nothing
# has answered, and bound the wait by the interface's own timeout.
_RESEND_WANT_CONFIG_S = 3.0


def _wait_connected_resending(iface: Any, timeout: float = 30.0) -> None:
    from meshtastic.mesh_interface import MeshInterface  # type: ignore[import-untyped]

    if iface.noProto:
        return
    deadline = time.monotonic() + min(timeout, float(iface._timeout.expireTimeout))
    while not iface.isConnected.wait(
        max(0.0, min(_RESEND_WANT_CONFIG_S, deadline - time.monotonic()))
    ):
        if time.monotonic() >= deadline:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for connection completion")
        if iface.myInfo is None:  # nothing of the config has arrived, so the request was lost
            log.info(
                "no reply to want_config on %s; sending it again", getattr(iface, "devPath", "?")
            )
            iface._startConfig()
    if iface.failure:
        raise iface.failure


def _resending(cls: Any) -> Any:
    """`cls` with a handshake wait that resends `want_config` and honors its timeout."""
    if not isinstance(cls, type):
        return cls  # a stand-in, as tests use

    class Resending(cls):  # type: ignore[misc,valid-type]
        def _waitConnected(self, timeout: float = 30.0) -> None:
            _wait_connected_resending(self, timeout)

    Resending.__name__ = cls.__name__
    return Resending


def _open_bounded(factory, port: str, deadline_s: float):
    """Run `factory()` (a blocking interface constructor) with a deadline.

    On timeout the worker is abandoned; if it ever returns an interface, that interface is
    closed so the port is released.
    """
    stuck = _wedged_opens.get(port)
    if stuck is not None:
        if stuck.is_alive():
            raise ConnectionError(
                f"Port {port} is still stuck in an earlier open that never returned. "
                "Power-cycle the board (uhubctl_cycle) and retry."
            )
        _wedged_opens.pop(port, None)

    box: dict = {}
    done = threading.Event()
    abandoned = threading.Event()

    def _run() -> None:
        try:
            box["iface"] = factory()
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()
            if abandoned.is_set() and "iface" in box:
                _close_bounded(box["iface"])

    worker = threading.Thread(target=_run, daemon=True, name=f"open-{port}")
    worker.start()
    if not done.wait(deadline_s):
        abandoned.set()
        _wedged_opens[port] = worker
        raise ConnectionError(
            f"Opening {port} did not return within {deadline_s:.0f}s; the board's USB serial "
            "looks wedged. Power-cycle it (uhubctl_cycle) and retry."
        )
    if "error" in box:
        raise box["error"]
    return box["iface"]


def close_bounded(iface) -> None:
    """Close a meshtastic interface without letting a wedged close hang the
    caller. Public for long-held interfaces (the nightly soak's API observers)
    that manage their own open/close lifecycle."""
    _close_bounded(iface)


def _close_bounded(iface) -> None:
    done = threading.Event()

    def _close() -> None:
        try:
            iface.close()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_close, daemon=True).start()
    if not done.wait(_CLOSE_TIMEOUT_S):
        log.warning("iface.close() did not return in %ss — abandoning it", _CLOSE_TIMEOUT_S)


class ConnectionError(RuntimeError):
    pass


def is_tcp_port(port: str | None) -> bool:
    return port is not None and port.startswith(TCP_SCHEME)


def parse_tcp_port(port: str) -> tuple[str, int]:
    """Parse `tcp://host[:port]` → (host, port). Defaults to 4403.

    Validates host shape (non-empty, no path separators) and port range
    (1..65535). Raises `ConnectionError` on malformed input — never lets
    a raw `ValueError` bubble up to a tool surface.
    """
    if not port.startswith(TCP_SCHEME):
        raise ConnectionError(f"Invalid TCP endpoint {port!r}: expected '{TCP_SCHEME}host[:port]'.")
    rest = port[len(TCP_SCHEME) :]
    if ":" in rest:
        host, port_str = rest.rsplit(":", 1)
        try:
            tcp_port = int(port_str)
        except ValueError as e:
            raise ConnectionError(
                f"Invalid TCP endpoint {port!r}: port {port_str!r} is not an integer."
            ) from e
    else:
        host, tcp_port = rest, DEFAULT_TCP_PORT
    if not host:
        raise ConnectionError(f"Invalid TCP endpoint {port!r}: empty host.")
    if any(c in host for c in ("/", "\\")):
        raise ConnectionError(
            f"Invalid TCP endpoint {port!r}: host {host!r} contains a path "
            "separator. TCP hostnames cannot contain '/' or '\\' — did you "
            "pass a serial port path or a Windows drive path by mistake?"
        )
    if not (1 <= tcp_port <= 65535):
        raise ConnectionError(
            f"Invalid TCP endpoint {port!r}: port {tcp_port} out of range (must be 1..65535)."
        )
    return host, tcp_port


def normalize_tcp_endpoint(endpoint: str) -> str:
    r"""Normalize `host`, `host:port`, or `tcp://host[:port]` → canonical
    `tcp://host:port` form. One place that owns the lock-key shape.

    Defers all validation to `parse_tcp_port`, so path-like inputs
    (`/dev/cu.foo`, `C:\Windows\…`), empty hosts, non-integer ports,
    and out-of-range ports raise `ConnectionError` here too.
    """
    if endpoint.startswith(TCP_SCHEME):
        canonical = endpoint
    elif ":" in endpoint:
        canonical = f"{TCP_SCHEME}{endpoint}"
    else:
        canonical = f"{TCP_SCHEME}{endpoint}:{DEFAULT_TCP_PORT}"
    host, port = parse_tcp_port(canonical)
    return f"{TCP_SCHEME}{host}:{port}"


def reject_if_tcp(port: str | None, tool_name: str) -> None:
    """Raise if `port` is a TCP endpoint — for tools that need real USB
    hardware (flash, bootloader, vendor escape hatches, serial monitor).

    Only checks the explicit arg; auto-selection via env var is the caller's
    responsibility to handle if it matters.
    """
    if is_tcp_port(port):
        raise ConnectionError(
            f"{tool_name} is not applicable to TCP/native nodes ({port}). "
            "This tool requires USB-attached hardware."
        )


def _remember(port: str, iface: Any) -> None:
    """Record what this connection saw for the device registry; never fails the connect."""
    try:
        my = getattr(iface, "myInfo", None)
        num = getattr(my, "my_node_num", None)
        user = ((getattr(iface, "nodesByNum", None) or {}).get(num) or {}).get("user") or {}
        device_names.record_seen(
            port,
            {
                "node_num": num,
                "model": user.get("hwModel"),
                "long_name": user.get("longName"),
                "short_name": user.get("shortName"),
            },
        )
    except Exception:
        log.debug("could not record %s in the device registry", port, exc_info=True)


def resolve_port(port: str | None) -> str:
    """Pick a port: explicit > sole likely_meshtastic candidate > error.

    A `tcp://` string passes through (after canonicalization). When `port`
    is None and no USB candidates are present, `MESHTASTIC_MCP_TCP_HOST`
    is consulted via `devices.list_devices()`.
    """
    if port:
        if is_tcp_port(port):
            return normalize_tcp_endpoint(port)
        if port.startswith(BLE_SCHEME):
            target = port[len(BLE_SCHEME) :]
            address = (
                device_names.ble_target(target) if device_names.looks_like_name(target) else None
            )
            return BLE_SCHEME + (address or target)
        if device_names.looks_like_name(port):
            attached = [d["port"] for d in devices.list_devices()]
            try:
                named = device_names.resolve(port, attached)
            except LookupError as exc:
                raise ConnectionError(f"{exc}. Run `devices` to see what is attached.") from exc
            if named is not None:
                return named
        return port
    candidates = [d for d in devices.list_devices() if d["likely_meshtastic"]]
    if not candidates:
        raise ConnectionError(
            "No Meshtastic devices detected. Plug one in, set "
            f"{TCP_HOST_ENV}=<host[:port]> for a meshtasticd daemon, "
            "or pass `port` explicitly. Run `list_devices` with "
            "include_unknown=True to see all serial ports."
        )
    if len(candidates) > 1:
        ports = ", ".join(c["port"] for c in candidates)
        raise ConnectionError(
            f"Multiple Meshtastic devices detected ({ports}). Specify `port` explicitly."
        )
    return candidates[0]["port"]


# Radios this process holds over BLE, by registry key. A serial open turns a radio's BLE off,
# so a serial open of one of these is refused, and a BLE connect to a radio held over serial.
_ble_held: dict[str, str] = {}
_serial_held: dict[str, str] = {}


def _device_key_or_none(port: str) -> str | None:
    from . import leases

    try:
        return leases.device_key(port)
    except Exception:
        return None


def _held_over_ble(port: str) -> str | None:
    key = _device_key_or_none(port)
    return _ble_held.get(key) if key is not None else None


@contextmanager
def _connect_ble(target: str, timeout_s: float, linger_s: float) -> Iterator:
    from meshtastic.ble_interface import BLEInterface  # type: ignore[import-untyped]

    ident = target[len(BLE_SCHEME) :]
    key = device_names.key_for_ble(ident)
    if key is not None and key in _serial_held:
        raise ConnectionError(
            f"{target} is open over serial ({_serial_held[key]}) in this server, and a serial "
            "connection turns the radio's BLE off. Use the serial port, or wait for it to close."
        )
    lock = registry.port_lock(target)
    if not lock.acquire(blocking=False):
        raise ConnectionError(
            f"{target} is busy: another device operation is in flight. Retry shortly."
        )
    iface = None
    ok = False
    try:
        # A BLE connect scans first (about 10 s) before the config handshake.
        iface = _open_bounded(
            lambda: _resending(BLEInterface)(address=ident, noProto=False, timeout=int(timeout_s)),
            target,
            timeout_s + 20.0,
        )
        # meshtastic-python keeps the address the scan matched on its bleak client.
        bleak_client = getattr(getattr(iface, "client", None), "bleak_client", None)
        address = getattr(bleak_client, "address", None) or ident
        num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
        if num is not None:
            try:
                rec = device_names.record_ble(num, address, ident if ident != address else None)
                key = rec["key"] if rec else key
            except Exception:
                log.debug("could not record the BLE identity of %s", target, exc_info=True)
        if key is not None:
            _ble_held[key] = target
        yield iface
        ok = True
    finally:
        if key is not None:
            _ble_held.pop(key, None)
        if ok and linger_s > 0:
            time.sleep(linger_s)
        if iface is not None:
            _close_bounded(iface)
        try:
            lock.release()
        except RuntimeError:
            pass


@contextmanager
def connect(port: str | None = None, timeout_s: float = 8.0, linger_s: float = 0.0) -> Iterator:
    """Open a meshtastic interface (serial or TCP) and always close it.

    For serial: raises `ConnectionError` immediately if another serial
    session holds the port (a `pio device monitor` in `serial_sessions/`).
    For TCP: no exclusive-access requirement, so the serial-session check
    is skipped — but the `port_lock` still serializes parallel `connect()`
    calls to the same daemon endpoint.

    `timeout_s` is plumbed through to both `SerialInterface(timeout=...)`
    and `TCPInterface(timeout=...)`. The meshtastic library uses the value
    as the reply-wait deadline for `localNode.waitForConfig()` during
    construction and for any subsequent admin RPC. `int()`-converted at
    the boundary because the upstream API expects whole seconds.

    `linger_s` delays close after a successful yield. Meshtastic firmware queues
    TX behind a channel-politeness delay (~4s for broadcasts); on immediate close
    the DTR reset kills the queued RF transmission before it reaches airtime, and
    can interrupt an admin flash write mid-commit. Set `linger_s` to let those
    finish. On exception, linger is skipped and close is immediate.

    Note the port lock is held for the whole linger, so a non-zero `linger_s`
    widens the window in which a concurrent caller gets the fail-fast busy error.
    Keep it at 0 for read-only ops. Clamped to `_MAX_LINGER_S` so a stray large
    value can't pin the port indefinitely.
    """
    resolved = resolve_port(port)
    timeout = int(timeout_s)
    linger_s = max(0.0, min(linger_s, _MAX_LINGER_S))

    if resolved.startswith(BLE_SCHEME):
        with _connect_ble(resolved, timeout_s, linger_s) as iface:
            yield iface
        return

    if is_tcp_port(resolved):
        from meshtastic.tcp_interface import (
            TCPInterface,  # type: ignore[import-untyped]
        )

        host, tcp_port = parse_tcp_port(resolved)
        lock = registry.port_lock(resolved)
        if not lock.acquire(blocking=False):
            raise ConnectionError(
                f"TCP endpoint {resolved} is busy — another device operation "
                "is in flight. Retry shortly."
            )

        iface = None
        ok = False
        try:
            iface = _resending(TCPInterface)(
                hostname=host,
                portNumber=tcp_port,
                connectNow=True,
                noProto=False,
                timeout=timeout,
            )
            _remember(resolved, iface)
            yield iface
            ok = True
        finally:
            # Linger only on success — a failed body has nothing queued worth
            # waiting for, and delaying the close would just widen the busy window.
            if ok and linger_s > 0:
                time.sleep(linger_s)
            if iface is not None:
                _close_bounded(iface)
            try:
                lock.release()
            except RuntimeError:
                pass
        return

    from meshtastic.serial_interface import (
        SerialInterface,  # type: ignore[import-untyped]
    )

    over_ble = _held_over_ble(resolved)
    if over_ble is not None:
        raise ConnectionError(
            f"{resolved} is held over BLE ({over_ble}) in this server, and a serial open would "
            "turn its BLE off. Wait for that connection to close."
        )

    active = registry.active_session_for_port(resolved)
    if active is not None:
        raise ConnectionError(
            f"Port {resolved} is held by serial session {active.id}. Call `serial_close` first."
        )

    lock = registry.port_lock(resolved)
    if not lock.acquire(blocking=False):
        raise ConnectionError(
            f"Port {resolved} is busy — another device operation is in flight. Retry shortly."
        )

    iface = None
    ok = False
    try:
        iface = _open_bounded(
            lambda: _resending(SerialInterface)(
                devPath=resolved,
                connectNow=True,
                noProto=False,
                timeout=timeout,
            ),
            resolved,
            timeout_s + _OPEN_GRACE_S,
        )
        _remember(resolved, iface)
        serial_key = _device_key_or_none(resolved)
        if serial_key is not None:
            _serial_held[serial_key] = resolved
        yield iface
        ok = True
    finally:
        # Linger only on success — a failed body has nothing queued worth
        # waiting for, and delaying the close would just widen the busy window.
        if ok and linger_s > 0:
            time.sleep(linger_s)
        if iface is not None:
            _close_bounded(iface)
        _serial_held.pop(_device_key_or_none(resolved) or "", None)
        try:
            lock.release()
        except RuntimeError:
            pass
