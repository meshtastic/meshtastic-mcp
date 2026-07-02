# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Make a serial port actually usable — unwedge it if it isn't.

Flashing (esptool / nrfutil / pio) and a clean ``connect()`` need an EXCLUSIVE
serial lock. A port can be unusable in two distinct ways, and they need
different remedies:

* **HELD** — another process/fd has the port open, so an exclusive open fails
  with ``[Errno 35]`` (EAGAIN). Common holders: a leaked ``SerialInterface``
  reader thread, a sibling ``meshtastic-mcp`` process, a stale ``pio device
  monitor``, or (historically) FleetSuite's serial monitor.
* **WEDGED** — the device firmware hung (often an interrupted flash). The
  ``/dev`` node goes stale and even a *non-exclusive* ``open()`` returns
  ``EINVAL`` (errno 22). No amount of waiting or lock juggling helps; only a USB
  power-cycle (re-enumerate the device) brings it back.

:func:`ensure_port_free` probes the port and escalates: brief wait → diagnose
the holder (``lsof``) → ``uhubctl`` power-cycle the device's **own hub slot**
(resolved from its USB topology, so it's correct on a bench with several
same-role devices, where a role→slot lookup is ambiguous) → wait for
re-enumeration → return the (possibly new) ``/dev`` path.

Shared by the test harness (``tests/test_00_bake.py``, ``conftest.py``) and
FleetSuite so unwedging behaves identically everywhere.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from . import uhubctl

log = logging.getLogger("meshtastic_mcp.port_recovery")


def port_openable(
    port: str, *, exclusive: bool = True, timeout: float = 0.5
) -> tuple[bool, BaseException | None]:
    """``(ok, exc)``: can we open ``port``? ``exclusive=True`` mirrors the lock
    esptool/nrfutil/pio take, so a True here means the flash will get the port."""
    try:
        import serial  # local import: pyserial is a runtime dep, keep module load light

        s = serial.Serial(port=port, exclusive=exclusive, timeout=timeout)
        s.close()
        return True, None
    except Exception as exc:
        return False, exc


def classify(exc: BaseException | None) -> str:
    """Human label for an open failure, by errno — so a message says WHICH mode
    it is. EINVAL(22) = stale/wedged CDC node (needs re-enumerate); EAGAIN/
    EADDRINUSE(35) = another fd holds the port. Decision logic never trusts
    errno alone (it varies by platform/pyserial) — this is for diagnostics."""
    errno = getattr(exc, "errno", None)
    if errno == 35:
        return "held (another process has the port open)"
    if errno == 22:
        return "wedged (device hung / stale node — needs USB re-enumerate)"
    if errno == 2:
        return "gone (no such device)"
    return "unopenable"


def who_holds_port(port: str) -> list[tuple[str, str]]:
    """Best-effort ``[(command, pid), …]`` holding an fd on ``port`` (via lsof).
    Empty list means nothing holds it (so an open failure is a *wedged* device,
    not contention) — or lsof is unavailable."""
    try:
        out = subprocess.run(["lsof", "-nP", port], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    holders: list[tuple[str, str]] = []
    for line in out.stdout.splitlines()[1:]:  # skip the header row
        parts = line.split()
        if len(parts) >= 2:
            holders.append((parts[0], parts[1]))
    return holders


def hub_slot_for_port(port: str) -> tuple[str | None, int | None]:
    """Map a ``/dev`` path to its uhubctl ``(hub_location, hub_port)`` via the USB
    topology pyserial exposes (location ``20-3.5`` → hub ``20-3``, port ``5``).
    Returns ``(None, None)`` if the port isn't enumerated or carries no location."""
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if p.device == port and getattr(p, "location", None):
                hub, _, sub = str(p.location).rpartition(".")
                if hub:
                    try:
                        return hub, int(sub)
                    except ValueError:
                        return None, None
        return None, None
    except Exception:
        return None, None


def port_on_slot(hub: str, hub_port: int) -> str | None:
    """The current ``/dev`` path enumerated on a given hub slot, or None. After a
    power-cycle the device may come back on a *different* ``/dev`` path but the
    same physical slot (location), so we re-find it by location."""
    loc = f"{hub}.{hub_port}"
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if getattr(p, "location", None) == loc:
                return p.device
    except Exception:
        pass
    return None


def _emit(msg: str) -> None:
    log.info(msg)
    # Also to stderr so it shows in pytest's live (-s) output, not just the
    # FleetSuite log.
    print(f"[unwedge] {msg}", file=sys.stderr, flush=True)


def ensure_port_free(
    port: str,
    *,
    role: str = "",
    wait_s: float = 8.0,
    poll: float = 0.25,
    allow_power_cycle: bool = True,
    cycle_delay_s: int = 2,
    reenum_timeout_s: float = 45.0,
) -> str:
    """Return a serial path that opens EXCLUSIVELY, power-cycling a wedged/held
    device if needed. The returned path MAY differ from ``port`` (the device can
    re-enumerate). Raises ``PortRecoveryError`` if the port can't be made usable.

    ``role`` is only used to label diagnostics. ``allow_power_cycle=False`` keeps
    it to a passive wait (no hardware action)."""
    label = f"{role}: " if role else ""

    # 1. Passive wait — a transient holder may release, or a just-rebooted
    #    device may finish settling.
    last_exc = _wait_openable(port, wait_s, poll)
    if last_exc is None:
        return port

    holders = who_holds_port(port)
    hub, hub_port = hub_slot_for_port(port)

    if not allow_power_cycle:
        raise PortRecoveryError(
            f"{label}{port} not exclusively openable after {wait_s:.0f}s "
            f"(last: {last_exc!r}; holders: {holders or 'none — wedged/EINVAL'}); "
            f"power-cycle disabled."
        )
    if hub is None or hub_port is None:
        raise PortRecoveryError(
            f"{label}{port} unusable (last: {last_exc!r}; holders: {holders or 'none'}) and "
            f"its hub slot can't be resolved from USB topology — cannot auto-recover. "
            f"Manual: `lsof {port}` to find a holder, or replug the device."
        )
    if not _uhubctl_available():
        raise PortRecoveryError(
            f"{label}{port} unusable and uhubctl isn't available to power-cycle "
            f"hub {hub}:{hub_port}. Install uhubctl or free the port manually "
            f"(holders: {holders or 'none — wedged device'})."
        )

    # 2. Power-cycle the device's OWN slot (correct even with several same-role
    #    devices), then re-find it by location and confirm it opens.
    _emit(
        f"{label}{port} unusable (last={last_exc!r}, holders={holders or 'none'}); "
        f"power-cycling hub {hub}:{hub_port}…"
    )
    try:
        uhubctl.cycle(hub, hub_port, delay_s=cycle_delay_s)
    except Exception as exc:
        raise PortRecoveryError(
            f"{label}power-cycle of hub {hub}:{hub_port} failed: {exc!r}"
        ) from exc

    time.sleep(0.5)  # VBUS is back; let the device start enumerating
    deadline = time.monotonic() + reenum_timeout_s
    last: BaseException | None = last_exc
    tried = port
    while time.monotonic() < deadline:
        tried = port_on_slot(hub, hub_port) or port
        ok, open_exc = port_openable(tried)
        if ok:
            _emit(f"{label}recovered on {tried}" + (f" (was {port})" if tried != port else ""))
            return tried
        last = open_exc
        time.sleep(poll)
    raise PortRecoveryError(
        f"{label}still unusable after power-cycling hub {hub}:{hub_port} "
        f"(tried {tried}, last: {last!r}). The device may need a manual replug."
    )


def ensure_port_responsive(
    port: str,
    *,
    role: str = "",
    health_timeout_s: float = 15.0,
    open_wait_s: float = 6.0,
    allow_power_cycle: bool = True,
    reenum_timeout_s: float = 45.0,
) -> str:
    """Like :func:`ensure_port_free`, but the device must ANSWER (a meshtastic
    config handshake), not merely open — for ``connect()``/admin flows where an
    openable-but-hung firmware is still useless. Power-cycles + re-resolves on
    failure. Returns the usable (possibly new) path, or raises ``PortRecoveryError``."""
    from . import recovery

    label = f"{role}: " if role else ""
    # First guarantee it at least opens (this already recovers a held/stale port).
    port = ensure_port_free(
        port, role=role, wait_s=open_wait_s, allow_power_cycle=allow_power_cycle
    )
    healthy, detail = recovery.is_healthy(port, timeout_s=health_timeout_s)
    if healthy:
        return port

    hub, hub_port = hub_slot_for_port(port)
    if not allow_power_cycle or hub is None or hub_port is None or not _uhubctl_available():
        raise PortRecoveryError(
            f"{label}{port} opens but the device doesn't answer ({detail}); "
            f"no power-cycle available to recover."
        )
    _emit(
        f"{label}{port} opens but is unresponsive ({detail}); power-cycling hub {hub}:{hub_port}…"
    )
    try:
        uhubctl.cycle(hub, hub_port, delay_s=2)
    except Exception as exc:
        raise PortRecoveryError(f"{label}power-cycle failed: {exc!r}") from exc

    time.sleep(0.5)
    deadline = time.monotonic() + reenum_timeout_s
    last: object = detail
    while time.monotonic() < deadline:
        cand = port_on_slot(hub, hub_port) or port
        ok, _ = port_openable(cand)
        if ok:
            healthy, last = recovery.is_healthy(cand, timeout_s=6.0)
            if healthy:
                _emit(f"{label}responsive again on {cand}")
                return cand
        time.sleep(0.3)
    raise PortRecoveryError(
        f"{label}device still unresponsive after power-cycling hub {hub}:{hub_port} "
        f"(last: {last}). May need a manual replug."
    )


def _wait_openable(port: str, wait_s: float, poll: float) -> BaseException | None:
    """Poll until ``port`` opens exclusively. Returns None on success, else the
    last open exception."""
    deadline = time.monotonic() + wait_s
    last_exc: BaseException | None = RuntimeError("not attempted")
    while time.monotonic() < deadline:
        ok, exc = port_openable(port)
        if ok:
            return None
        last_exc = exc
        time.sleep(poll)
    return last_exc


def _uhubctl_available() -> bool:
    from . import config

    try:
        config.uhubctl_bin()
        return True
    except Exception:
        return False


class PortRecoveryError(RuntimeError):
    """A serial port could not be made exclusively usable, even after escalation."""


__all__ = [
    "PortRecoveryError",
    "classify",
    "ensure_port_free",
    "ensure_port_responsive",
    "hub_slot_for_port",
    "port_on_slot",
    "port_openable",
    "who_holds_port",
]
