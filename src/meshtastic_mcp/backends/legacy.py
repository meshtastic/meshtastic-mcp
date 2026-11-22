# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The pre-1.0 result shapes, served by the backends.

The `legacy` tools, the web control plane, recovery and the hardware suite call these; each is
one backend call reshaped, so every device read and write takes the same path as `inspect`,
`configure`, `send` and `admin`.
"""

from __future__ import annotations

from typing import Any

from . import BackendError, for_target
from .protopath import LOCAL_CONFIG_SECTIONS, MODULE_CONFIG_SECTIONS

TIMEOUT_S = 30.0


def _no_snapshot(_port: str, _config: dict[str, Any]) -> dict[str, Any]:
    return {}


def device_info(port: str | None = None, timeout_s: float = 8.0) -> dict[str, Any]:
    got = for_target(port).read(port, ["summary"], None, "full", timeout_s)
    s = got["summary"]
    return {
        "port": got.get("target"),
        "my_node_num": s.get("node_num"),
        "long_name": s.get("long_name"),
        "short_name": s.get("short_name"),
        "firmware_version": s.get("firmware_version"),
        "hw_model": s.get("hw_model"),
        "region": s.get("region"),
        "num_nodes": s.get("num_nodes", 0),
        "primary_channel": s.get("primary_channel"),
        "modem_preset": s.get("modem_preset"),
        "channel_num": s.get("channel_num"),
        "display": s.get("display"),
    }


def list_nodes(port: str | None = None, timeout_s: float = 8.0) -> list[dict[str, Any]]:
    from .python import node_record

    got = for_target(port).read(port, ["nodes"], None, "full", timeout_s)
    return [node_record(n) for n in got.get("nodes") or []]


def get_config(section: str | None = None, port: str | None = None) -> dict[str, Any]:
    whole = section is None or section == "all"
    if not whole and section not in LOCAL_CONFIG_SECTIONS | MODULE_CONFIG_SECTIONS:
        valid = sorted(LOCAL_CONFIG_SECTIONS | MODULE_CONFIG_SECTIONS)
        raise BackendError(f"Unknown config section: {section!r}. Valid sections: {valid}")
    sections = None if whole else [str(section)]
    got = for_target(port).read(port, ["config"], sections, "brief", 10.0)
    config = got.get("config") or {}
    if not whole:
        return {"config": {section: config.get(section)}}
    return {
        "config": {
            "localConfig": {k: v for k, v in config.items() if k in LOCAL_CONFIG_SECTIONS},
            "moduleConfig": {k: v for k, v in config.items() if k in MODULE_CONFIG_SECTIONS},
        }
    }


def _configure(port: str | None, changes: dict[str, Any]) -> dict[str, Any]:
    return for_target(port).configure(port, changes, None, False, TIMEOUT_S, _no_snapshot)


def set_config(path: str, value: Any, port: str | None = None) -> dict[str, Any]:
    section = path.split(".", 1)[0]
    if section not in LOCAL_CONFIG_SECTIONS | MODULE_CONFIG_SECTIONS:
        raise BackendError(f"Unknown config section: {section!r}")
    result = _configure(port, {path: value})
    change = (result.get("changes") or {}).get(path) or {"from": value, "to": value}
    return {
        "ok": result.get("verified") is not False,
        "path": path,
        "section": section,
        "parent": "localConfig" if section in LOCAL_CONFIG_SECTIONS else "moduleConfig",
        "old_value": change.get("from"),
        "new_value": change.get("to"),
    }


def set_owner(long_name: str, short_name: str | None = None, port: str | None = None) -> dict:
    if short_name is not None and len(short_name) > 4:
        raise BackendError("short_name must be 4 characters or fewer")
    changes: dict[str, Any] = {"owner.long_name": long_name}
    if short_name is not None:
        changes["owner.short_name"] = short_name
    result = _configure(port, changes)
    return {
        "ok": result.get("verified") is not False,
        "long_name": long_name,
        "short_name": short_name,
    }


def set_debug_log_api(enabled: bool, port: str | None = None) -> dict[str, Any]:
    result = _configure(port, {"security.debug_log_api_enabled": bool(enabled)})
    return {"ok": result.get("verified") is not False, "debug_log_api_enabled": bool(enabled)}


def get_channel_url(include_all: bool = False, port: str | None = None) -> dict[str, Any]:
    got = for_target(port).read(port, ["url"], None, "brief", 10.0)
    urls = got.get("url") or {}
    return {"url": urls.get("all" if include_all else "primary")}


def set_channel_url(url: str, port: str | None = None) -> dict[str, Any]:
    result = for_target(port).import_channels(port, url, False, 10.0)
    return {"ok": True, "channels_imported": result.get("channels_imported")}


def send_text(
    text: str,
    to: str | int | None = None,
    channel_index: int = 0,
    want_ack: bool = False,
    port: str | None = None,
    tx_linger_s: float | None = None,
) -> dict[str, Any]:
    result = for_target(port).send(
        port, to, {"text": text}, channel_index, want_ack, None, 30.0, tx_linger_s
    )
    return {
        "ok": True,
        "packet_id": result.get("packet_id"),
        "destination": to if to is not None else "^all",
    }


def _admin(port: str | None, action: str, seconds: int) -> dict[str, Any]:
    return for_target(port).admin(port, action, None, seconds, False, TIMEOUT_S)


def reboot(port: str | None = None, confirm: bool = False, seconds: int = 10) -> dict[str, Any]:
    if not confirm:
        raise BackendError("reboot is destructive and requires confirm=True.")
    _admin(port, "reboot", seconds)
    return {"ok": True, "rebooting_in_s": seconds}


def factory_reset(
    port: str | None = None, confirm: bool = False, full: bool = False
) -> dict[str, Any]:
    if not confirm:
        raise BackendError("factory_reset is destructive and requires confirm=True.")
    _admin(port, "factory_reset_full" if full else "factory_reset", 0)
    return {"ok": True, "full": full}


def send_input_event(
    event_code: int | str,
    kb_char: int = 0,
    touch_x: int = 0,
    touch_y: int = 0,
    port: str | None = None,
) -> dict[str, Any]:
    from ..input_events import coerce_event_code

    code = coerce_event_code(event_code)
    if not 0 <= kb_char <= 255:
        raise ValueError(f"kb_char out of u8 range: {kb_char}")
    if not 0 <= touch_x <= 65535:
        raise ValueError(f"touch_x out of u16 range: {touch_x}")
    if not 0 <= touch_y <= 65535:
        raise ValueError(f"touch_y out of u16 range: {touch_y}")
    return for_target(port).input_event(port, code, kb_char, touch_x, touch_y)
