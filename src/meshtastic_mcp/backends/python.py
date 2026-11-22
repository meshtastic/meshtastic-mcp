# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The meshtastic-python backend: serial and TCP radios, in this process."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable, Collection
from typing import Any

from meshtastic.protobuf import admin_pb2, localonly_pb2

from .. import connection, display_mirror
from . import BackendError, library_guard
from .protodict import to_dict


def _sections(descriptor: Any) -> tuple[str, ...]:
    return tuple(f.name for f in descriptor.fields if f.message_type is not None)


LOCAL_SECTIONS = _sections(localonly_pb2.LocalConfig.DESCRIPTOR)
MODULE_SECTIONS = _sections(localonly_pb2.LocalModuleConfig.DESCRIPTOR)
SECTIONS = LOCAL_SECTIONS + MODULE_SECTIONS


def _enum_name(msg: Any, field: str) -> str | None:
    try:
        value = getattr(msg, field)
        return str(msg.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number[value].name)
    except (AttributeError, KeyError):
        return None


def primary_channel_name(iface: Any) -> str | None:
    """The primary channel's name on a connected interface; "(default)" when unnamed."""
    try:
        channels = iface.localNode.channels or []
    except AttributeError:
        return None
    for ch in channels:
        if getattr(ch, "role", None) == 1:  # PRIMARY
            name = getattr(getattr(ch, "settings", None), "name", None)
            return name or "(default)"
    return None


def region_name(iface: Any) -> str | None:
    """The LoRa region's enum name on a connected interface."""
    local = getattr(iface, "localNode", None)
    if local is None or getattr(local, "localConfig", None) is None:
        return None
    return _enum_name(local.localConfig.lora, "region")


def node_record(n: dict[str, Any]) -> dict[str, Any]:
    """One node-DB entry in the flat shape the tools report."""
    user = n.get("user") or {}
    position = n.get("position") or None
    metrics = n.get("deviceMetrics") or {}
    return {
        "node_num": n.get("num"),
        "user": {
            "long_name": user.get("longName"),
            "short_name": user.get("shortName"),
            "hw_model": user.get("hwModel"),
            "role": user.get("role"),
        },
        "position": (
            {
                "latitude": position.get("latitude"),
                "longitude": position.get("longitude"),
                "altitude": position.get("altitude"),
                "time": position.get("time"),
            }
            if position
            else None
        ),
        "snr": n.get("snr"),
        "rssi": n.get("rssi"),
        "last_heard": n.get("lastHeard"),
        "battery_level": metrics.get("batteryLevel"),
        "is_favorite": bool(n.get("isFavorite", False)),
    }


def _summary(iface: Any, detail: str) -> dict[str, Any]:
    my = iface.myInfo
    num = getattr(my, "my_node_num", None)
    user = ((iface.nodesByNum or {}).get(num) or {}).get("user") or {}
    lora = iface.localNode.localConfig.lora
    out: dict[str, Any] = {
        "node_num": num,
        "node_id": f"!{num:08x}" if isinstance(num, int) else None,
        "long_name": user.get("longName"),
        "short_name": user.get("shortName"),
        "hw_model": user.get("hwModel"),
        "role": _enum_name(iface.localNode.localConfig.device, "role"),
        "firmware_version": getattr(iface.metadata, "firmware_version", None),
        "region": _enum_name(lora, "region"),
        "modem_preset": _enum_name(lora, "modem_preset"),
        "channel_num": lora.channel_num,
        "primary_channel": primary_channel_name(iface),
        "num_nodes": len(iface.nodesByNum or {}),
    }
    if detail == "full":
        out["metadata"] = to_dict(iface.metadata) if iface.metadata is not None else None
        out["my_info"] = to_dict(my) if my is not None else None
        out["display"] = display_mirror.display_info(iface.metadata)
    return out


def _config(iface: Any, sections: Collection[str] | None) -> dict[str, Any]:
    node = iface.localNode
    out: dict[str, Any] = {}
    for section in sections or SECTIONS:
        parent = node.localConfig if section in LOCAL_SECTIONS else node.moduleConfig
        # A section the radio never sent is null, not a section of defaults.
        out[section] = to_dict(getattr(parent, section)) if parent.HasField(section) else None
    return out


def _channels(iface: Any) -> list[dict[str, Any]]:
    out = []
    for ch in iface.localNode.channels or []:
        entry = to_dict(ch)
        if entry.get("role") == "DISABLED":
            continue
        out.append(entry)
    return out


def _node_brief(n: dict[str, Any]) -> dict[str, Any]:
    rec = node_record(n)
    user = rec.pop("user")
    rec.pop("position", None)
    num = rec.get("node_num")
    return {
        "node_num": num,
        "node_id": f"!{num:08x}" if isinstance(num, int) else None,
        **user,
        "hops_away": n.get("hopsAway"),
        **rec,
    }


def _node_full(n: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in n.items() if k != "raw"}


def _nodes(iface: Any, detail: str) -> list[dict[str, Any]]:
    shape = _node_full if detail == "full" else _node_brief
    return [shape(n) for n in (iface.nodesByNum or {}).values()]


def _ip(value: int) -> str | None:
    # The firmware copies the address in network byte order into a little-endian fixed32.
    return socket.inet_ntoa(struct.pack("<I", value)) if value else None


def _network(iface: Any, timeout_s: float) -> dict[str, Any]:
    done = threading.Event()
    box: dict[str, Any] = {}

    def on_response(packet: dict[str, Any]) -> None:
        decoded = packet.get("decoded") or {}
        raw = (decoded.get("admin") or {}).get("raw")
        if raw is not None and raw.HasField("get_device_connection_status_response"):
            box["status"] = raw.get_device_connection_status_response
        else:
            reason = (decoded.get("routing") or {}).get("errorReason")
            box["error"] = f"the radio answered with {reason or 'no connection status'}"
        done.set()

    request = admin_pb2.AdminMessage(get_device_connection_status_request=True)
    iface.localNode._sendAdmin(request, wantResponse=True, onResponse=on_response)
    if not done.wait(timeout_s):
        raise BackendError(f"the radio sent no connection status within {timeout_s:g} s")
    if "error" in box:
        raise BackendError(box["error"])
    status = box["status"]
    out: dict[str, Any] = {}
    for link in ("wifi", "ethernet", "bluetooth", "serial"):
        if not status.HasField(link):
            out[link] = None  # the radio has no such interface
            continue
        entry = to_dict(getattr(status, link))
        inner = entry.get("status")
        if isinstance(inner, dict):
            inner["ip_address"] = _ip(getattr(status, link).status.ip_address)
        out[link] = entry
    return out


class PythonBackend:
    name = "python"
    parts = frozenset({"summary", "config", "channels", "nodes", "network", "url", "contact_url"})

    def read(
        self,
        target: str | None,
        parts: Collection[str],
        sections: Collection[str] | None,
        detail: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with library_guard(), connection.connect(port=target, timeout_s=timeout_s) as iface:
            out["target"] = getattr(iface, "devPath", None) or target
            readers = {
                "summary": lambda: _summary(iface, detail),
                "config": lambda: _config(iface, sections),
                "channels": lambda: _channels(iface),
                "nodes": lambda: _nodes(iface, detail),
                "network": lambda: _network(iface, timeout_s),
                "url": lambda: {
                    "primary": iface.localNode.getURL(includeAll=False),
                    "all": iface.localNode.getURL(includeAll=True),
                },
                "contact_url": lambda: iface.localNode.getContactURL(iface.myInfo.my_node_num),
            }
            for part in parts:
                try:
                    out[part] = readers[part]()
                except BackendError as exc:
                    errors[part] = str(exc)
        if errors:
            out["errors"] = errors
        return out

    def configure(
        self,
        target: str | None,
        changes: dict[str, Any] | None,
        restore: dict[str, Any] | None,
        dry_run: bool,
        timeout_s: float,
        save_snapshot: Callable[[str, dict[str, Any]], dict[str, Any]],
        dest: str | int | None = None,
    ) -> dict[str, Any]:
        from .python_configure import configure

        with library_guard():
            return configure(target, changes, restore, dry_run, timeout_s, save_snapshot, dest)

    def admin(
        self,
        target: str | None,
        action: str,
        node_id: str | int | None,
        seconds: int,
        wait_back: bool,
        timeout_s: float,
        dest: str | int | None = None,
        value: Any = None,
    ) -> dict[str, Any]:
        from .python_admin import admin

        with library_guard():
            return admin(target, action, node_id, seconds, wait_back, timeout_s, dest, value)

    def send(
        self,
        target: str | None,
        to: str | int | None,
        payload: dict[str, Any],
        channel: int,
        want_ack: bool,
        hop_limit: int | None,
        wait_s: float,
        linger_s: float | None = None,
    ) -> dict[str, Any]:
        from .python_send import send

        with library_guard():
            return send(target, to, payload, channel, want_ack, hop_limit, wait_s, linger_s)

    def import_channels(
        self, target: str | None, url: str, add_only: bool, timeout_s: float
    ) -> dict[str, Any]:
        if "/#" not in url:
            raise BackendError(f"{url!r} is not a channel URL (https://meshtastic.org/e/#...)")
        if add_only and "?add=true#" not in url:
            url = url.replace("/#", "/?add=true#", 1)  # setURL(addOnly) reads that form
        with (
            library_guard(),
            connection.connect(port=target, timeout_s=timeout_s, linger_s=2.5) as iface,
        ):
            node = iface.localNode
            before = sum(1 for c in node.channels or [] if getattr(c, "role", 0) != 0)
            node.setURL(url, addOnly=add_only)
            after = sum(1 for c in node.channels or [] if getattr(c, "role", 0) != 0)
            port = getattr(iface, "devPath", None) or target
        out: dict[str, Any] = {"ok": True, "target": port, "channels_imported": after}
        if add_only:
            out["channels_added"] = after - before
        return out

    def input_event(
        self, target: str | None, event_code: int, kb_char: int, touch_x: int, touch_y: int
    ) -> dict[str, Any]:
        msg = admin_pb2.AdminMessage()
        msg.send_input_event.event_code = event_code
        msg.send_input_event.kb_char = kb_char
        msg.send_input_event.touch_x = touch_x
        msg.send_input_event.touch_y = touch_y
        with library_guard(), connection.connect(port=target) as iface:
            iface.localNode._sendAdmin(msg)  # meshtastic-python has no public wrapper
        return {"ok": True, "event_code": event_code, "kb_char": kb_char}

    def raw_config(self, target: str | None, timeout_s: float) -> dict[str, Any]:
        from .python_configure import full_snapshot

        with library_guard(), connection.connect(port=target, timeout_s=timeout_s) as iface:
            return full_snapshot(iface)
