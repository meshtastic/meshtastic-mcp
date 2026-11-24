# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Answer a call to a hidden or removed tool with the tool that replaces it.

A client that cached its tool list before a server upgrade, or an agent trained on the 0.x
names, calls `device_info` and would otherwise get only `Unknown tool`. The names stay out of
`tools/list`; only a call by name gets the pointer.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import Middleware

from . import toolsets

# Each `legacy` tool and the call that replaces it.
REPLACED_BY: dict[str, str] = {
    "device_info": "inspect(node=...)",
    "list_nodes": 'inspect(node=..., what=["nodes"])',
    "get_config": 'inspect(node=..., what=["<section>"])',
    "get_channel_url": 'inspect(node=..., what=["url"])',
    "set_config": 'configure(node=..., changes={"<section>.<field>": value})',
    "set_owner": 'configure(node=..., changes={"owner.long_name": ...})',
    "set_debug_log_api": 'configure(node=..., changes={"security.debug_log_api_enabled": True})',
    "send_text": "send(node=..., text=...)",
    "reboot": 'admin(action="reboot")',
    "shutdown": 'admin(action="shutdown")',
    "factory_reset": 'admin(action="factory_reset") or admin(action="factory_reset_full")',
    "esptool_raw": 'vendor(tool="esptool", args=[...])',
    "nrfutil_raw": 'vendor(tool="nrfutil", args=[...])',
    "picotool_raw": 'vendor(tool="picotool", args=[...])',
    "replay_start": 'sim(action="start", ...)',
    "replay_stop": 'sim(action="stop")',
    "replay_status": "sim_status()",
    "replay_inject": "inject(session_id, kind, ...)",
    "replay_inject_beacon": 'inject(session_id, "beacon", ...)',
    "replay_inject_client_notification": 'inject(session_id, "client_notification", ...)',
    "replay_inject_fileinfo": 'inject(session_id, "fileinfo", ...)',
    "replay_inject_traceroute": 'inject(session_id, "traceroute", ...)',
    "replay_inject_waypoint": 'inject(session_id, "waypoint", ...)',
    "list_devices": "devices(include_unknown=True)",
    "events_window": 'query(sql="SELECT * FROM events WHERE ts > epoch(now()) - 900")',
    "logs_window": 'query(sql="SELECT * FROM logs WHERE ts > epoch(now()) - 900")',
    "packets_window": 'query(sql="SELECT * FROM packets WHERE ts > epoch(now()) - 900")',
    "telemetry_timeline": 'query(sql="SELECT time, from_node, fields FROM telemetry ...")',
    "triage_bundle": "query(sql=...) over the packets, logs and events views",
    "drive_display_poll": "job(job_id, wait_s=...)",
    "screenshot_set_poll": "job(job_id, wait_s=...)",
    "build_start": 'build(env=...), or install(port=..., source={"env": ...})',
    "build_poll": "job(job_id, wait_s=...)",
    "flash_start": 'install(port=..., source={"env": ...})',
    "flash_poll": "job(job_id, wait_s=...)",
    "pio_flash": 'install(port=..., source={"env": ...})',
    "update_flash": 'install(port=..., source={"env": ...})',
    "erase_and_flash": 'install(port=..., source={"env": ...}, erase=True)',
    "esptool_chip_info": 'vendor(tool="esptool", args=["chip_id"], port=...)',
    "esptool_erase_flash": 'vendor(tool="esptool", args=["erase_flash"], port=...)',
    "nrfutil_dfu": 'install(port=..., source={"file": "<package>.zip"})',
    "picotool_info": 'vendor(tool="picotool", args=["info", "-a"])',
    "picotool_load": 'vendor(tool="picotool", args=["load", "-x", "-t", "uf2", "<file>.uf2"])',
    "nrf52_flash_uf2": 'install(port=..., source={"file": "<file>.uf2"})',
    "serial_list": "devices(include_unknown=True)",
    "serial_read": 'query(sql="SELECT * FROM logs WHERE port = ... ORDER BY ts DESC")',
    "ble_sniff_poll": "job(job_id, wait_s=...)",
    "vanity_grind_poll": "job(job_id, wait_s=...)",
    "sdk_device_info": "inspect(node=...) with MESHTASTIC_MCP_BACKEND=sdk",
    "sdk_list_nodes": 'inspect(node=..., what=["nodes"]) with MESHTASTIC_MCP_BACKEND=sdk',
    "sdk_send_text": "send(node=..., text=...) with MESHTASTIC_MCP_BACKEND=sdk",
    "sdk_status": "doctor()",
    "replay_fuzz_presets": "sim_status(fuzz_presets=True)",
    "android_tap": 'app(platform="android", action="tap", ...)',
    "android_swipe": 'app(platform="android", action="swipe", ...)',
    "android_type_text": 'app(platform="android", action="type_text", ...)',
    "android_clear_logcat": 'app(platform="android", action="clear_logcat")',
    "android_screenshot": 'app_view(platform="android", action="screenshot")',
    "android_ui_dump": 'app_view(platform="android", action="ui_dump")',
    "android_find_text": 'app_view(platform="android", action="find_text", token=...)',
    "android_poll_for_text": 'app_view(platform="android", action="poll_for_text", token=...)',
    "android_read_logcat": 'app_view(platform="android", action="read_logcat")',
    "android_resolve": 'app_view(platform="android", action="resolve", label=...)',
    "ios_boot": 'app(platform="ios", action="boot")',
    "ios_open_url": 'app(platform="ios", action="open_url", url=...)',
    "ios_tap": 'app(platform="ios", action="tap", x=..., y=...)',
    "ios_type_text": 'app(platform="ios", action="type_text", text=...)',
    "ios_ui_dump": 'app_view(platform="ios", action="ui_dump")',
}

_LOCAL_MODEL = frozenset(
    {
        "local_model_status",
        "local_model_serve",
        "local_model_serve_stop",
        "summarize_window",
        "vision_oracle",
        "triage_window",
    }
)


def pointer(name: str) -> str | None:
    """Why `name` is not callable here and what to call instead, or None for an unknown name."""
    if name in REPLACED_BY:
        return (
            f"{name} is a pre-1.0 tool this server hides by default. Call "
            f"{REPLACED_BY[name]} instead, or set {toolsets.TOOLSETS_ENV}=all,legacy on the "
            "server and restart it to get the old tool back."
        )
    group = toolsets.toolset_of(name)
    if group in toolsets.OPT_IN and group != toolsets.LEGACY:
        return (
            f"{name} is in the {group!r} toolset, which this server leaves out by default. "
            f"Set {toolsets.TOOLSETS_ENV}=all (or add {group}) on the server and restart it."
        )
    if name.startswith("discord_"):
        return (
            f"{name} moved to the separate meshtastic-mcp-discord server; register that server "
            "to use it."
        )
    if name in _LOCAL_MODEL:
        return f"{name} was removed in 1.0; run the local model outside this server."
    return None


class RetiredToolMiddleware(Middleware):
    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except NotFoundError:
            hint = pointer(getattr(context.message, "name", ""))
            if hint is None:
                raise
            raise ToolError(hint) from None
