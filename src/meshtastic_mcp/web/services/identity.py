"""Device identity reconciliation.

Two jobs: (1) map a USB VID to a coarse *role* and a role to a default pio
*env*, and resolve the precise env from a board's hw_model when we know it;
(2) derive a *stable key* for a device so a unit with a real serial number is
tracked across ports, while a serial-less unit gets a (port-derived) surrogate
that is explicitly NOT stable.
"""

from __future__ import annotations

import hashlib

from meshtastic_mcp import boards

# USB vendor IDs we recognise → coarse role. Case-insensitive; compared as the
# lowercased hex string (e.g. "0x239a").
_VID_ROLE = {
    "0x239a": "nrf52",  # Adafruit / RAK nRF52840
    "0x303a": "esp32s3",  # Espressif native USB
    "0x10c4": "esp32s3",  # Silicon Labs CP210x UART bridge
    "0x1a86": "esp32s3",  # WCH CH340/CH9102 UART bridge
}

# Coarse role → the default pio env to fall back on when we can't resolve the
# exact board variant from its hw_model.
_ROLE_ENV = {
    "nrf52": "rak4631",
    "esp32s3": "heltec-v3",
}

# UART-bridge VIDs (CP210x, CH34x, FTDI, PL2303). Their USB serial number is
# unreliable — many ship a shared default like "0001" — so devices behind these
# bridges are keyed by physical USB location instead of serial, and marked
# non-stable. Native-USB boards (nRF 0x239a, ESP32-S3 0x303a) have real per-chip
# serials and keep serial-keying.
_BRIDGE_VIDS = {"0x10c4", "0x1a86", "0x0403", "0x067b"}

_NOSERIAL_PREFIX = "noserial:"


def role_for_vid(vid: str | None) -> str | None:
    if not vid:
        return None
    return _VID_ROLE.get(vid.lower())


def env_for_role(role: str | None) -> str | None:
    if not role:
        return None
    return _ROLE_ENV.get(role)


def env_for_hw_model(hw_model: str | None) -> str | None:
    """Resolve the exact pio env for a hardware model slug (e.g. ``HELTEC_V4``).

    Prefers the *base* env (``heltec-v4``) over decorated variants
    (``heltec-v4-tft``): when several envs declare the same hw_model slug, the
    one whose name is the canonical slugification wins, else the shortest.
    Returns None for an unknown slug.
    """
    if not hw_model:
        return None
    target = hw_model.upper()
    candidates = [
        b["env"]
        for b in boards.list_boards()
        if (b.get("hw_model_slug") or "").upper() == target and b.get("env")
    ]
    if not candidates:
        return None
    canonical = hw_model.lower().replace("_", "-")
    if canonical in candidates:
        return canonical
    return min(candidates, key=len)


def device_key(d: dict) -> tuple[str, bool]:
    """Return ``(key, is_stable)`` for a discovered-device dict.

    A trustworthy serial number (native-USB board, not a UART bridge) is a
    stable key that follows the device across ports. Otherwise — no serial, or a
    bridge chip whose serial may be a shared default — we synthesise a
    ``noserial:<hash>`` surrogate from the physical USB location (falling back to
    the port path), so two identical bridges on different ports stay distinct.
    Such a key is NOT stable across replug, which the UI surfaces.
    """
    serial = d.get("serial_number")
    vid = (d.get("vid") or "").lower()
    if serial and vid not in _BRIDGE_VIDS:
        return str(serial), True
    basis = d.get("location") or d.get("port") or ""
    raw = f"{vid}:{d.get('pid')}:{basis}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:12]
    return f"{_NOSERIAL_PREFIX}{digest}", False


def has_stable_id(key: str | None) -> bool:
    return bool(key) and not str(key).startswith(_NOSERIAL_PREFIX)


def hub_slot_from_location(location: str | None) -> tuple[str, int] | None:
    """Derive the uhubctl ``(hub_location, port)`` from a device's USB topology
    string (pyserial's ``ListPortInfo.location``), which the OS already tracks.

    The last dotted segment is the port on the immediately-upstream hub; the rest
    is that hub's location — exactly uhubctl's scheme. So ``20-3.5`` → ``("20-3",
    5)`` and the deeper ``1-1.3.2`` → ``("1-1.3", 2)``. This is deterministic and
    unambiguous even for identical-VID boards — no power-cycling needed.

    Returns None when the location isn't a hub-port path (no dotted port).
    """
    if not location:
        return None
    # Linux appends a ":config.interface" suffix (e.g. "1-1.3:1.0") — drop it.
    loc = str(location).split(":", 1)[0]
    hub, sep, port = loc.rpartition(".")
    if not sep or not port.isdigit():
        return None
    return hub, int(port)
