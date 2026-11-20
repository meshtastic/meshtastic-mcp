# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The `FastMCP` app, the detected capabilities, and the decorators that gate a tool on one.

The app lives here rather than in `server` so every tool module can register onto it
without importing `server`, which imports them.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .. import __version__ as _pkg_version
from .. import capabilities
from .. import tasks as tasks_mod

# Report our package version in the MCP handshake; otherwise the SDK falls back to
# advertising its own version (`pkg_version("mcp")`) as the server version.
app = FastMCP("meshtastic-mcp", version=_pkg_version)
tasks_mod.install(app)


# Capability detection drives conditional tool registration. The portable core always
# registers; firmware-coupled tools (build/flash/boards/userprefs) register only when a
# firmware checkout is present, so a `pip install meshtastic-mcp` with no firmware tree
# still exposes the full device/admin/recorder surface instead of advertising tools that
# would error on every call.
CAPS = capabilities.detect()


def firmware_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the firmware capability is active.

    Without a firmware tree the handler is still defined (so imports/tests are unaffected)
    but is not advertised to MCP clients.
    """

    def deco(fn):
        if CAPS.firmware:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def android_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the android capability is active.

    Without the `android` CLI + `adb` the Android tools are not advertised so
    they cannot be called and cannot error on a missing binary.
    """

    def deco(fn):
        if CAPS.android:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def apple_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the apple capability is active.

    Without `xcrun` (+ optional `idb`) the Apple tools are not advertised.
    """

    def deco(fn):
        if CAPS.apple:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def sdr_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the sdr capability is active.

    Without `pyrtlsdr` + an attached RTL-SDR, the RF-compliance oracle tools
    (`rf_scan`, `rf_confirm_tx`) are not advertised. See `doctor` for what's missing.
    """

    def deco(fn):
        if CAPS.sdr:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def ble_sniffer_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when a BLE sniffer dongle is attached.

    Gates the capture tools only. `ble_sniff_status` is core, for the same reason
    `pa_meter_status` is: the tool that reports the hardware missing must not be
    hidden by the hardware being missing.
    """

    def deco(fn):
        if CAPS.ble_sniffer:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def sdk_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the Kotlin SDK CLI is present.

    Gates the experimental device-IO bridge tools that shell out to the
    meshtastic-sdk headless JVM CLI (an alternative to the Python `meshtastic`
    library); plain installs without the CLI are unaffected.
    """

    def deco(fn):
        if CAPS.sdk_cli:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def discord_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when a Discord bot token resolves.

    Gates the read-only Discord source tools; installs without a token never
    advertise them. Content they return is untrusted (public community server).
    """

    def deco(fn):
        if CAPS.discord:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def vanity_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but only registers when the `mvgrind` grinder is present.

    Gates the GPU grind tools only. `vanity_preview` and `vanity_apply` are core:
    a key ground on another machine is still inspectable and applicable here.
    """

    def deco(fn):
        if CAPS.mvgrind:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco


def app_plane_tool(*args: Any, **kwargs: Any):
    """Like `@app.tool()` but registers when the android or apple capability is active."""

    def deco(fn):
        if CAPS.android or CAPS.apple:
            return app.tool(*args, **kwargs)(fn)
        return fn

    return deco
