# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""USB hub power control for tests — thin composition of the `uhubctl`
module + `_port_discovery.resolve_port_by_role`.

Why separate from the production module:
- `meshtastic_mcp.uhubctl.cycle` returns as soon as uhubctl exits (VBUS is
  back on, but the device hasn't finished enumerating as a CDC port yet).
- Tests that want to immediately issue a `connect(port=...)` need the NEW
  `/dev/cu.*` path, which can differ from the pre-cycle path on nRF52
  boards (CDC re-enumeration assigns a fresh `cu.usbmodemNNNN`).
- `resolve_port_by_role` already handles that wait + path-resolution for
  the `factory_reset` flow. Composing the two gives a one-call helper.

Also exposes `is_uhubctl_available()` so fixtures can skip cleanly when
uhubctl isn't installed — we never want "no uhubctl" to look like a test
failure.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from meshtastic_mcp import config as config_mod
from meshtastic_mcp import uhubctl as uhubctl_mod

from ._port_discovery import resolve_port_by_role


def is_uhubctl_available() -> bool:
    """Return True iff `config.uhubctl_bin()` resolves AND the binary is callable.

    Soft-fails silently — fixtures use this to `pytest.skip` with an
    actionable message when the operator hasn't installed uhubctl.
    """
    try:
        config_mod.uhubctl_bin()
    except Exception:
        return False
    # Do NOT actually invoke uhubctl here — on macOS a non-sudo run would
    # fail, which is a config issue, not a tool-missing issue. That gets
    # surfaced to the user when they actually run a recovery action.
    return True


def power_on(role: str, *, resolved: tuple[str, int] | None = None) -> dict[str, Any]:
    """Power on the hub port hosting `role`. Does NOT wait for re-enumeration.
    Use `power_cycle` or follow with `resolve_port_by_role` to block on readiness.

    Pass `resolved=(location, port)` to skip the VID lookup — essential when the
    device is currently powered OFF (and so invisible to `resolve_target`, which
    would raise). Resolve once while it's still up and reuse it for on/off.
    """
    loc, port = resolved or uhubctl_mod.resolve_target(role)
    return uhubctl_mod.power_on(loc, port)


def power_off(role: str, *, resolved: tuple[str, int] | None = None) -> dict[str, Any]:
    """Power off the hub port hosting `role`. The device disappears from
    `list_devices` immediately. Pass `resolved=(location, port)` to skip the VID
    lookup (see `power_on`)."""
    loc, port = resolved or uhubctl_mod.resolve_target(role)
    return uhubctl_mod.power_off(loc, port)


def hub_cuts_power(
    role: str,
    *,
    absence_timeout_s: float = 8.0,
) -> bool:
    """Probe whether the hub ACTUALLY cuts VBUS on `role`'s port.

    A genuinely non-switching hub reports the device as STILL attached to its
    port after an off (VBUS never dropped) — such a hub can't run the
    peer-offline tier, so we skip it. Absence is read from the hub's own connect
    flag (`uhubctl.device_on_port`), NOT OS enumeration: on macOS a powered-off
    device lingers in `ioreg`/`system_profiler`/`/dev` as a zombie, so the OS
    view would falsely report every hub as non-switching. Cut the port, watch
    the hub flag, restore power (always). Returns True iff the hub dropped the
    device.

    The target resolves once up-front, while the device is still visible —
    `resolve_target` can't find a powered-off device.
    """
    resolved = uhubctl_mod.resolve_target(role)
    power_off(role, resolved=resolved)
    try:
        wait_for_absence(role, timeout_s=absence_timeout_s, resolved=resolved)
        return True
    except TimeoutError:
        return False
    finally:
        power_on(role, resolved=resolved)


def power_cycle(
    role: str,
    *,
    delay_s: int = 2,
    rediscover_timeout_s: float = 30.0,
) -> str:
    """Cycle the port hosting `role`, wait for re-enumeration, return the
    new port path.

    On nRF52 the post-cycle path typically matches the pre-cycle path, but
    macOS may assign a different `/dev/cu.usbmodemNNNN` if the previous
    CDC endpoint hasn't been fully released. `resolve_port_by_role`
    handles that transparently.
    """
    loc, port = uhubctl_mod.resolve_target(role)
    uhubctl_mod.cycle(loc, port, delay_s=delay_s)
    # After uhubctl exits, VBUS is on but the device may still be in
    # bootloader init. Give it ~500 ms head-start before polling so we
    # don't spam list_devices pointlessly.
    time.sleep(0.5)
    return resolve_port_by_role(role, timeout_s=rediscover_timeout_s)


def recover_absent_role(role: str, *, settle_timeout_s: float = 25.0) -> str | None:
    """Power-cycle ``role``'s pinned hub slot to wake a wedged/off board,
    returning its new ``/dev`` path once it enumerates, or None if the slot
    stays empty.

    nRF52 native-USB boards can drop off the bus after a prior tier's
    ``power_off`` and stay absent — a wedged CDC needs a VBUS cycle to
    re-enumerate. The pinned-slot ``resolve_target`` path (env pins seeded by
    ``conftest.pytest_configure``) addresses the slot even while the device is
    invisible, so this works on an absent board.

    Best-effort — never raises. An empty slot (no board), a missing hub, or a
    hub that needs sudo yields None; a genuinely wedged device that a VBUS cycle
    revives yields its path. Used by the session ``bench_wake`` step so a board
    that dropped off the bus doesn't silently disable a whole tier by being
    absent when ``hub_devices`` snapshots.
    """
    try:
        return power_cycle(role, rediscover_timeout_s=settle_timeout_s)
    except Exception:
        return None


def wait_for_absence(
    role: str,
    *,
    timeout_s: float = 20.0,
    expected_port: str | None = None,
    resolved: tuple[str, int] | None = None,
) -> None:
    """Block until the device under test is confirmed GONE after a ``power_off``.

    Prefer the HUB signal: pass ``resolved=(hub_location, port)`` (resolved
    while the device is still up — a powered-off device can't be VID-resolved)
    and absence is read from the hub's own connect flag via
    ``uhubctl.device_on_port``. This is reliable and immediate. The legacy
    ``list_devices`` path (used only when ``resolved`` is None) checks OS USB
    enumeration, which on macOS keeps a ZOMBIE of a powered-off device for an
    unbounded time — so it can time out even though power was really cut. That
    was the real cause of ``peer_offline_recovery`` failing on a hub that DOES
    switch power.

    ``expected_port`` only applies to the legacy path. Raises TimeoutError.
    """
    deadline = time.monotonic() + timeout_s

    if resolved is not None:
        loc, port = resolved
        while time.monotonic() < deadline:
            if not uhubctl_mod.device_on_port(loc, port):
                return
            time.sleep(0.2)
        raise TimeoutError(
            f"role {role!r} still attached to hub {loc}:{port} {timeout_s}s after power_off"
        )

    from meshtastic_mcp import devices as devices_mod

    from ._port_discovery import _ROLE_VIDS, _coerce_vid  # type: ignore[attr-defined]

    if role not in _ROLE_VIDS:
        raise ValueError(f"unknown role {role!r}")
    wanted = _ROLE_VIDS[role]
    while time.monotonic() < deadline:
        found = devices_mod.list_devices(include_unknown=True)
        if expected_port is not None:
            if not any(d.get("port") == expected_port for d in found):
                return
        elif not any(_coerce_vid(d.get("vid")) in wanted for d in found):
            return
        time.sleep(0.3)
    raise TimeoutError(
        f"role {role!r} (port {expected_port}) still visible after {timeout_s}s of power_off"
    )


def drain_port_fd(port: str, *, timeout_s: float = 8.0) -> bool:
    """Best-effort: wait for any lingering serial fd on `port` to close, and clear
    any leaked in-process port lock, so a following VBUS cut can actually tear down
    the device's CDC node.

    A meshtastic `device_info`/`connect` can leave an fd open on a daemon
    close-thread (`connection._close_bounded` abandons a slow close after 5s). On
    macOS a held fd pins the USB-serial node in the IORegistry, so the device keeps
    showing up in `list_devices` even after its hub slot loses power — which is
    exactly what makes `wait_for_absence` time out. Draining the fd first lets
    `power_off` + `wait_for_absence` behave as intended.

    Returns True once the port opens exclusively (nothing holds it), False on
    timeout. Never raises — purely advisory.
    """
    from meshtastic_mcp import registry
    from meshtastic_mcp.port_recovery import port_openable, who_holds_port

    # Drop any leaked in-process per-port lock (e.g. an abandoned connect thread).
    try:
        registry.clear_port_lock(port)
    except Exception:
        pass

    deadline = time.monotonic() + timeout_s
    warned = False
    while time.monotonic() < deadline:
        ok, _ = port_openable(port, exclusive=True, timeout=0.5)
        if ok:
            return True
        if not warned and any(pid == str(os.getpid()) for _cmd, pid in who_holds_port(port)):
            warned = True  # log the diagnostic once, not every poll
            print(
                f"[power-cycle-test] {port}: our own process still holds an fd "
                f"(lingering close-thread) — waiting for it to drain…",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(0.3)
    return False


__all__ = [
    "drain_port_fd",
    "is_uhubctl_available",
    "power_cycle",
    "power_off",
    "power_on",
    "recover_absent_role",
    "wait_for_absence",
]
