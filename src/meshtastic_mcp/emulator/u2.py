# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Optional uiautomator2 fast path for Android UI driving (the ``[android-fast]`` extra).

One-shot ``adb shell uiautomator dump`` cold-starts an instrumentation process
per call (~1-3 s) and fails outright on continuously-animating screens
("could not get idle state"). uiautomator2 keeps a resident server on the
device and talks to it over an adb-forwarded socket, making hierarchy dumps
effectively instantaneous and text input Unicode-safe (its ``send_keys`` uses
clipboard+paste, avoiding ``input text``'s character mangling — the source of
stray-keystroke bugs when tapping near the IME).

Import-guarded like :mod:`meshtastic_mcp.replay.tak`: :func:`available` is the
gate, every consumer falls back to the plain-adb path on any failure, so the
core stays dependency-light and behavior without the extra is unchanged.

Install: ``uv tool install 'meshtastic-mcp[android-fast]'``.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_devices: dict[str, Any] = {}


def available() -> bool:
    """True when uiautomator2 is importable (the ``[android-fast]`` extra)."""
    try:
        import uiautomator2  # noqa: F401

        return True
    except Exception:
        return False


def device(serial: str | None = None) -> Any:
    """A cached uiautomator2 device handle (connecting pushes/starts the
    on-device server on first use — cache so that cost is paid once)."""
    import uiautomator2

    key = serial or ""
    with _lock:
        d = _devices.get(key)
        if d is None:
            d = uiautomator2.connect(serial) if serial else uiautomator2.connect()
            _devices[key] = d
        return d


def reset(serial: str | None = None) -> None:
    """Drop the cached handle (e.g. after a device reboot)."""
    with _lock:
        _devices.pop(serial or "", None)


def dump(serial: str | None = None) -> list[dict[str, Any]]:
    """View hierarchy in avd.ui_dump's dict schema.

    The resident server's ``dump_hierarchy()`` returns the same uiautomator XML
    as ``adb exec-out uiautomator dump``, so we hand it to the exact same parser
    the plain-adb fallback uses — one labeling scheme, so a label picked from a
    fast-path dump still resolves after a fallback re-dump.
    """
    from . import avd  # local import: avd imports u2 at module level (cycle)

    xml_text = device(serial).dump_hierarchy()
    return avd._parse_uiautomator_xml(xml_text)


def tap(x: int, y: int, serial: str | None = None) -> None:
    device(serial).click(x, y)


def send_keys(text: str, serial: str | None = None) -> None:
    """Type into the focused field. Unicode-safe (clipboard-paste under the
    hood) — no ``%s`` space encoding or metacharacter mangling.

    When uiautomator2 falls back to its clipboard-paste path (no FastInputIME),
    the typed text is left on the device clipboard. Clear it afterwards so
    caller-supplied text (which may be sensitive) doesn't linger. Best-effort:
    a clipboard-clear failure never masks a successful type."""
    d = device(serial)
    try:
        d.send_keys(text)
    finally:
        # Wipe only the paste buffer (NOT the field — clear_text() would delete
        # what we just typed). Best-effort across u2 versions/IME modes.
        try:
            d.set_clipboard("")
        except Exception:
            pass


def tap_text(token: str, serial: str | None = None, *, timeout: float = 5.0) -> bool:
    """Atomic find-then-tap: locate the element by exact text at action time and
    click it. Avoids acting on stale coordinates from an earlier dump (layouts
    shift — e.g. when the soft keyboard opens). Returns False if not found."""
    d = device(serial)
    el = d(text=token)
    if el.wait(timeout=timeout):
        el.click()
        return True
    return False
