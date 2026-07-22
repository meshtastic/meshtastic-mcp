# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capability detection for the standalone meshtastic-mcp server.

The server has a portable **core** (device discovery, serial+TCP transport, admin,
recorder/observability, input-events, uhubctl, vendor escape hatches) that always
works against any connected device, plus optional **capabilities** that light up only
when their prerequisite is present:

- ``firmware`` — build / clean / flash / OTA / board enum / userPrefs. Needs a
  Meshtastic firmware checkout (``MESHTASTIC_FIRMWARE_ROOT`` or a ``platformio.ini``
  above cwd) and PlatformIO.
- ``android`` — Android emulator + native-node orchestration for hardware-free e2e.
  Needs the ``android`` CLI and ``adb`` on PATH.
- ``apple`` — iOS Simulator / macOS-app + native-node orchestration. Needs ``xcrun``
  (``idb`` for UI drive).

Tool registration in ``server.py`` consults these so a ``pip install meshtastic-mcp``
with no firmware tree still exposes the full device/admin/recorder surface.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from . import config


def has_firmware() -> bool:
    """True when a firmware tree is resolvable (build/flash tools are usable)."""
    return config.firmware_root_or_none() is not None


def has_pio() -> bool:
    try:
        config.pio_bin()
        return True
    except config.ConfigError:
        return False


def has_android() -> bool:
    """True when the Android CLI + adb are available (Android-emulator e2e is usable)."""
    return shutil.which("android") is not None and shutil.which("adb") is not None


def has_apple() -> bool:
    """True when xcrun is available (iOS Simulator / macOS app e2e is usable)."""
    return shutil.which("xcrun") is not None


def has_sdr() -> bool:
    """True when `pyrtlsdr` is importable and at least one RTL-SDR is attached.

    Gates the RF-compliance oracle tools (`rf_scan`, `rf_confirm_tx`): an
    independent on-air check of a device's configured region/preset/frequency
    against what it actually transmits. Needs the ``sdr`` extra
    (``pip install 'meshtastic-mcp[sdr]'``) plus ``librtlsdr`` on the system
    (e.g. ``apt install librtlsdr-dev rtl-sdr``).
    """
    from . import sdr

    try:
        return len(sdr.list_devices()) > 0
    except Exception:
        # Capability detection must never crash server startup — a missing
        # pyrtlsdr/numpy, an old system librtlsdr (ctypes AttributeError), or
        # a wedged USB stack all just mean "no sdr capability".
        return False


def has_local_model() -> bool:
    """True when a local Ollama instance is reachable (offload tools are usable).

    Optional: enables the bulk-text offload tools (summarize/triage recorder
    windows, narrate packets) that push token-heavy work onto a local GPU.
    """
    from . import local_model

    return local_model.available()


def has_llama_server() -> bool:
    """True when a ``llama``/``llama-server`` binary is on PATH (can run/manage one).

    Gates the bootstrap tools that start a self-contained llama.cpp backend; the
    offload tools themselves gate on ``has_local_model`` (a reachable backend).
    """
    from . import llama_server

    return llama_server.available()


def has_power_meter() -> bool:
    """True when an ImmersionRC RF Power Meter v2 is attached (VID 0x04D8/PID 0x000A).

    Gates the PA-calibration bench tools (`pa_meter_status`, `pa_measure`,
    `pa_sweep`): absolute TX-power measurement off a node's PA. Needs no extra
    (the driver is pure `pyserial`, already a core dep) — only the meter plugged
    in and powered on. It auto-powers-off on a battery timeout and vanishes from
    USB, so a meter that was off at server startup won't light up the capability.
    """
    from . import power_meter

    return len(power_meter.list_meters()) > 0


def has_tak() -> bool:
    """True when the meshtastic-tak SDK is importable (the ``[tak]`` extra).

    Optional: enables real TAKPacketV2 zstd-dictionary wire compression for the
    replay sim's ATAK squad (``profile tak.wire="v2"``). Install with
    ``pip install 'meshtastic-mcp[tak]'``.
    """
    from .replay import tak

    return tak.available()


def has_sdk_cli() -> bool:
    """True when the Meshtastic Kotlin SDK ``cli`` launcher is resolvable.

    Optional: enables the experimental device-IO tools that shell out to the
    Kotlin SDK's headless JVM CLI (BLE/TCP/serial engine) as an alternative to
    the Python ``meshtastic`` library. Resolution is path-only (no JVM spawn).
    """
    from . import sdk_cli

    return sdk_cli.available()


@dataclass(frozen=True)
class Capabilities:
    firmware: bool
    pio: bool
    android: bool
    apple: bool
    local_model: bool
    llama_server: bool
    sdr: bool
    power_meter: bool
    tak: bool
    sdk_cli: bool

    def summary(self) -> str:
        active = [
            n
            for n, on in (
                ("firmware", self.firmware),
                ("pio", self.pio),
                ("android", self.android),
                ("apple", self.apple),
                ("local_model", self.local_model),
                ("llama_server", self.llama_server),
                ("sdr", self.sdr),
                ("power_meter", self.power_meter),
                ("tak", self.tak),
                ("sdk_cli", self.sdk_cli),
            )
            if on
        ]
        return ", ".join(active) if active else "core-only"


def detect() -> Capabilities:
    return Capabilities(
        firmware=has_firmware(),
        pio=has_pio(),
        android=has_android(),
        apple=has_apple(),
        local_model=has_local_model(),
        llama_server=has_llama_server(),
        sdr=has_sdr(),
        power_meter=has_power_meter(),
        tak=has_tak(),
        sdk_cli=has_sdk_cli(),
    )
