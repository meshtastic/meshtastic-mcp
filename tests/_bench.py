"""Canonical bench board registry — the single source of truth mapping each
physical board to its test role, PlatformIO env, USB VID(s), and hub-slot
location.

Why this exists: the reference bench (USB hub ``20-3``) hosts four distinct
boards, and *three of them share the nRF52 native-USB VID 0x239a* — so they
cannot be told apart by VID. Every layer that used to key roles by VID with
"first match wins" silently collapsed those three into a single ``nrf52`` role,
baked the wrong firmware onto whichever board enumerated first, and left the
other two unprovisioned.

Hub-slot **location** is the stable discriminator: it survives the
app↔bootloader USB PID flip (a board in DFU advertises a different PID) and
tells otherwise-identical boards apart. Discovery, the bake, post-DFU
re-resolution, and power-cycle recovery all bind by location.

The reference bench, hub ``20-3``::

    slot 1  LilyGO T-Echo Plus       (0x239a)  -> env t-echo-plus
    slot 2  Heltec T114 / HT-n5262   (0x239a)  -> env heltec-mesh-node-t114
    slot 5  Heltec V3  (esp32s3)     (0x10c4)  -> env heltec-v3   (CP2102 UART)
    slot 7  WisCore RAK4631          (0x239a)  -> env rak4631

Override for a different bench with ``--hub-profile=<yaml>`` (role → {vid,
location, env}) or per-role env vars: ``MESHTASTIC_UHUBCTL_LOCATION_<ROLE>`` +
``MESHTASTIC_UHUBCTL_PORT_<ROLE>`` (power/recovery) and
``MESHTASTIC_MCP_ENV_<ROLE>`` (firmware env).
"""

from __future__ import annotations

# role -> {vid, alt_vids, location, env}. Keep this the ONLY place these four
# facts are written down; conftest.hub_profile, conftest.pytest_generate_tests,
# _port_discovery._ROLE_VIDS, and test_00_bake all derive from it.
BENCH_ROLES: dict[str, dict] = {
    "t_echo": {
        # LilyGO T-Echo Plus (per bench records + the prebuilt env). Its USB
        # bootloader advertises the string "T-Echo v1" — that's the bootloader
        # label, not the board variant. If this slot actually holds a *plain*
        # T-Echo, override with MESHTASTIC_MCP_ENV_T_ECHO=t-echo.
        "vid": 0x239A,
        "alt_vids": (),
        "location": "20-3.1",
        "env": "t-echo-plus",
    },
    "heltec_t114": {
        "vid": 0x239A,
        "alt_vids": (),
        "location": "20-3.2",
        "env": "heltec-mesh-node-t114",
    },
    "esp32s3": {
        # Heltec V3 enumerates via a CP2102 (0x10c4); a native-USB ESP32-S3
        # would be 0x303a. Accept both so the role is portable.
        "vid": 0x10C4,
        "alt_vids": (0x303A,),
        "location": "20-3.5",
        "env": "heltec-v3",
    },
    "rak4631": {
        "vid": 0x239A,
        "alt_vids": (),
        "location": "20-3.7",
        "env": "rak4631",
    },
}


def roles() -> list[str]:
    """Canonical role order (drives parametrization ids)."""
    return list(BENCH_ROLES)


def role_vids(role: str) -> tuple[int, ...]:
    """All USB VIDs that can identify ``role`` (primary + alternates)."""
    spec = BENCH_ROLES[role]
    return (spec["vid"], *spec.get("alt_vids", ()))


def role_location(role: str) -> str | None:
    """Hub-slot location string (e.g. ``"20-3.1"``) for ``role``, or None."""
    return BENCH_ROLES.get(role, {}).get("location")


def role_env(role: str) -> str | None:
    """Default PlatformIO env name for ``role``, or None."""
    return BENCH_ROLES.get(role, {}).get("env")


def role_envs() -> dict[str, str]:
    """``role -> env`` for every role that pins an env."""
    return {r: s["env"] for r, s in BENCH_ROLES.items() if s.get("env")}


def hub_profile() -> dict[str, dict]:
    """The default ``hub_profile`` shape consumed by conftest: role -> {vid,
    pid_contains, location, env}. ``pid_contains`` stays None (we disambiguate
    by location, not PID)."""
    return {
        role: {
            "vid": spec["vid"],
            "alt_vids": tuple(spec.get("alt_vids", ())),
            "pid_contains": None,
            "location": spec.get("location"),
            "env": spec.get("env"),
        }
        for role, spec in BENCH_ROLES.items()
    }


def location_hub_port(location: str | None) -> tuple[str, int] | None:
    """Split a location string (``"20-3.1"``) into the uhubctl ``(hub, port)``
    pair (``("20-3", 1)``), or None if it can't be parsed."""
    if not location:
        return None
    hub, _, slot = location.rpartition(".")
    if not hub:
        return None
    try:
        return hub, int(slot)
    except ValueError:
        return None


def role_for_hub_slot(hub_location: str | None, hub_port: int | None) -> str | None:
    """Reverse of :func:`role_location` / :func:`location_hub_port`: the bench
    role occupying a given uhubctl ``(hub_location, port)`` slot, or None if no
    role sits there.

    This is the discriminator that tells the three same-VID 0x239a nRF52 boards
    apart from the *web* side: a registry device pins its physical slot in
    ``hub_location`` / ``hub_port`` (set from USB topology at discovery), and that
    slot — not the collapsible VID — is what maps it back to a distinct role."""
    if not hub_location or hub_port is None:
        return None
    try:
        want = (str(hub_location), int(hub_port))
    except (TypeError, ValueError):
        return None
    for role, spec in BENCH_ROLES.items():
        if location_hub_port(spec.get("location")) == want:
            return role
    return None


def device_location(port: str) -> str | None:
    """Hub-slot location string for a live ``/dev`` path (``"20-3.1"``), or None
    if the port isn't enumerated or carries no USB location."""
    from meshtastic_mcp import port_recovery

    hub, slot = port_recovery.hub_slot_for_port(port)
    if hub is None or slot is None:
        return None
    return f"{hub}.{slot}"
