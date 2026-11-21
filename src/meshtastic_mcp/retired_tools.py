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
    "device_info": 'inspect(node=...)',
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
