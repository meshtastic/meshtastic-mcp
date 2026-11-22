# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The `FastMCP` app, the detected capabilities, and the decorators that gate a tool on one.

The app lives here rather than in `server` so every tool module can register onto it
without importing `server`, which imports them.
"""

from __future__ import annotations

from collections.abc import Callable
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


# Tool names each capability gates, recorded whether or not it is active, so the startup log
# and `doctor` can name what an inactive capability leaves out.
GATED: dict[str, list[str]] = {}


def _gated(capability: str, active: Callable[[], bool]) -> Callable[..., Any]:
    def factory(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        def deco(fn: Any) -> Any:
            GATED.setdefault(capability, []).append(fn.__name__)
            return app.tool(*args, **kwargs)(fn) if active() else fn

        return deco

    return factory


# Without the capability the handler is still defined, so imports and tests are unaffected,
# but it is not advertised and cannot fail on a missing binary.
firmware_tool = _gated("firmware", lambda: CAPS.firmware)
android_tool = _gated("android", lambda: CAPS.android)
apple_tool = _gated("apple", lambda: CAPS.apple)
sdr_tool = _gated("sdr", lambda: CAPS.sdr)
# The capture tools only; `ble_sniff_status` stays core so it can report the dongle missing.
ble_sniffer_tool = _gated("ble_sniffer", lambda: CAPS.ble_sniffer)
sdk_tool = _gated("sdk_cli", lambda: CAPS.sdk_cli)
# The GPU grind only; `vanity_preview` and `vanity_apply` stay core.
vanity_tool = _gated("mvgrind", lambda: CAPS.mvgrind)
app_plane_tool = _gated("app_plane", lambda: CAPS.android or CAPS.apple)
