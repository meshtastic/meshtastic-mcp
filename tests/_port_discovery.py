# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Role-to-port rediscovery after USB CDC re-enumeration.

Used by tests that mutate device identity in ways macOS treats as a
"new device" — notably ``factory_reset(full=False)`` on the nRF52840 and
any operation that kicks the device through its bootloader. Both cases
cause the kernel to re-assign the ``/dev/cu.usbmodem*`` path; a test that
captured the pre-operation port and reuses it after will fail with
``FileNotFoundError``.

The helper polls :func:`meshtastic_mcp.devices.list_devices` (the same API
``run-tests.sh`` and ``conftest.py::hub_devices`` use for initial hub
detection) filtered by the role's canonical USB VID. Returns the first
matching port — equivalent to "give me the single nRF52 (or ESP32-S3) on
the bench right now, whichever `cu.*` path it happens to be at".

Test-harness-local (not exported from ``meshtastic_mcp``): a thin wrapper
over public ``devices.list_devices`` with no extra moving parts. If a
non-test caller ever needs this, it's trivial to promote.

Caveat: the session-scoped ``hub_devices`` fixture snapshots ports at
session start and is dict-keyed — it doesn't learn about re-enumerations.
Tests that call ``resolve_port_by_role`` should use the returned port
locally for the rest of the test body rather than expecting
``hub_devices[role]`` to update.
"""

from __future__ import annotations

import os
import time

from meshtastic_mcp import devices as devices_module

from . import _bench

# Role → canonical VID(s), derived from the single source of truth in
# `tests/_bench.py`. With three same-VID (0x239a) nRF52 boards on the bench, VID
# alone is ambiguous — `resolve_port_by_role` prefers each role's pinned hub-slot
# location and only falls back to VID for roles with no location.
_ROLE_VIDS: dict[str, tuple[int, ...]] = {role: _bench.role_vids(role) for role in _bench.roles()}


def _coerce_vid(raw: object) -> int | None:
    """`devices.list_devices` returns vid as either '0x239a' or an int;
    normalize to int. None on un-parseable input (matches the same fault-
    tolerance `run-tests.sh` uses for its role detection)."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        except ValueError:
            return None
    return None


def _role_location(role: str) -> str | None:
    """Effective hub-slot location for ``role``, env pins first.

    ``MESHTASTIC_UHUBCTL_LOCATION_<ROLE>`` + ``MESHTASTIC_UHUBCTL_PORT_<ROLE>``
    are the same per-role pin channel ``uhubctl.resolve_target`` honors for
    power/recovery (``conftest.pytest_configure`` seeds them from the active
    bench profile), so on a non-default bench re-resolution follows the pinned
    slot rather than the reference registry in ``tests/_bench.py``. Falls back
    to the registry location when no pin is set."""
    hub = os.environ.get(f"MESHTASTIC_UHUBCTL_LOCATION_{role.upper()}")
    slot = os.environ.get(f"MESHTASTIC_UHUBCTL_PORT_{role.upper()}")
    if hub and slot:
        return f"{hub}.{slot}"
    return _bench.role_location(role)


def _settled_openable(port: str, open_timeout_s: float) -> bool:
    """True iff ``port`` opens EXCLUSIVELY twice across a short gap.

    The signal that an nRF52 native-USB CDC has stopped re-enumerating. After a
    VBUS cut the device can re-appear on a ``/dev/cu.*`` path that
    ``list_devices`` reports but that immediately vanishes — a caller who opens
    it a moment later gets ``could not open port ... No such file`` (the exact
    run-32 ``esp32s3->t_echo`` failure). A bare presence check can't tell a
    flapping path from a settled one; a successful exclusive open can, and
    requiring two across a gap rejects a path that opens once then drops."""
    from meshtastic_mcp import port_recovery

    ok, _ = port_recovery.port_openable(port, exclusive=True, timeout=open_timeout_s)
    if not ok:
        return False
    time.sleep(0.4)
    ok, _ = port_recovery.port_openable(port, exclusive=True, timeout=open_timeout_s)
    return ok


def resolve_port_by_role(
    role: str,
    *,
    timeout_s: float = 30.0,
    poll_start: float = 0.5,
    poll_max: float = 5.0,
    require_openable: bool = False,
    open_timeout_s: float = 1.0,
) -> str:
    """Return the current ``/dev/cu.*`` path for ``role`` once one appears.

    Polls ``devices.list_devices(include_unknown=True)`` every ``poll_start``
    seconds (1.5× backoff, capped at ``poll_max``) until a device matching
    ``role``'s VID appears. Returns the first matching port.

    On timeout raises :class:`AssertionError` with the list of devices that
    WERE seen — helpful when debugging "wrong board connected" vs. "no
    board connected" vs. "still re-enumerating".

    Args:
        role: a key of ``_ROLE_VIDS`` (e.g. ``"rak4631"``, ``"esp32s3"``), or
            any role pinned via ``MESHTASTIC_UHUBCTL_LOCATION_<ROLE>`` +
            ``MESHTASTIC_UHUBCTL_PORT_<ROLE>`` (a ``--hub-profile`` bench).
        timeout_s: upper bound on how long to wait for the device to
            re-appear. Default 30 s — nRF52 factory_reset observed at
            2-12 s on a healthy lab hub.
        poll_start: initial poll interval in seconds. Default 0.5 s.
        poll_max: cap on poll interval after backoff. Default 5 s.
        require_openable: when True, a matched path is only returned once it
            opens exclusively (see :func:`_settled_openable`). A still-flapping
            nRF52 re-enumeration is skipped and polling continues, so callers
            that immediately open the port (``connect`` / ``SerialInterface``)
            never receive a path that vanishes on open. Default False (bare
            presence, back-compatible).
        open_timeout_s: per-open timeout for the ``require_openable`` probe.

    Raises:
        AssertionError: if no matching device appears (and, when
            ``require_openable``, settles openable) within ``timeout_s``.
        ValueError: if ``role`` is neither in ``_ROLE_VIDS`` nor pinned to a
            hub slot via env vars.

    """
    location = _role_location(role)
    if role not in _ROLE_VIDS and location is None:
        raise ValueError(
            f"unknown role {role!r}; expected one of {sorted(_ROLE_VIDS)}, or pin its "
            f"slot via MESHTASTIC_UHUBCTL_LOCATION_{role.upper()} + "
            f"MESHTASTIC_UHUBCTL_PORT_{role.upper()}"
        )
    wanted_vids = _ROLE_VIDS.get(role, ())

    deadline = time.monotonic() + timeout_s
    delay = poll_start
    last_seen: list[dict] = []
    while time.monotonic() < deadline:
        try:
            last_seen = devices_module.list_devices(include_unknown=True)
        except Exception as exc:
            # list_devices is wrapped by meshtastic_mcp.devices and
            # shouldn't raise on normal enumeration — but a kernel-level
            # USB hiccup during re-enumeration can bubble up briefly.
            # Treat as "nothing seen this round" and retry.
            last_seen = [{"error": repr(exc)}]
        for dev in last_seen:
            port = dev.get("port")
            if not port:
                continue
            if location is not None:
                # Prefer the exact physical hub slot — stable across the
                # app↔bootloader USB PID flip and unambiguous when several
                # boards share a VID. Do NOT fall back to VID here: we want
                # THIS board, not any same-VID sibling.
                if _bench.device_location(port) != location:
                    continue
            else:
                vid = _coerce_vid(dev.get("vid"))
                if vid is None or vid not in wanted_vids:
                    continue
            # Matched the role. When the caller will immediately open the port,
            # gate on the CDC having actually settled — a flapping nRF52
            # re-enumeration lists here but vanishes on open.
            if require_openable and not _settled_openable(port, open_timeout_s):
                continue
            return port
        time.sleep(delay)
        delay = min(delay * 1.5, poll_max)

    # Timeout path — include what we saw so the operator can tell
    # "nothing plugged in" from "wrong board" from "transient USB error".
    where = f"location {location!r}" if location else f"VIDs {[hex(v) for v in wanted_vids]}"
    raise AssertionError(
        f"no device matching role {role!r} ({where}) appeared within "
        f"{timeout_s:.0f}s. Last enumeration: {last_seen!r}"
    )
