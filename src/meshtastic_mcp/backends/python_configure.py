# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Config writes over meshtastic-python: snapshot, apply, then read every change back.

The firmware acknowledges writes it drops, so a write counts only once a fresh connection reads
it back. Several writes go in one edit transaction, paced, because an unpaced burst loses
writes and can lose the transaction's own begin. A commit always restarts the radio; a single
write restarts it unless the section applies live (`lora`, and some `device`, `power` and
`display` fields). `restarted` is measured from the radio's uptime, not predicted: the
reboot counter in `MyNodeInfo` only counts on ESP32, and reads 0 on nRF52.
"""

from __future__ import annotations

import functools
import inspect
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import Message
from meshtastic.protobuf import localonly_pb2, portnums_pb2, telemetry_pb2

from .. import connection
from . import BackendError, protopath
from .protodict import to_dict

PACE_S = 0.25  # between writes inside a transaction
REBOOT_DELAY_S = 7.0  # firmware DEFAULT_REBOOT_SECONDS after a save that restarts
BOOT_S = 3.0  # time for the radio to come back far enough to answer
UPTIME_SLACK_S = 5.0  # clock and reply jitter allowed before a lower uptime counts as a restart
OWNER_KEYS = ("owner.long_name", "owner.short_name")
OWNER_FLAGS = ("owner.is_licensed", "owner.is_unmessagable")
_CHANNEL = re.compile(r"^channel\.(\d)\.(.+)$")
_IDENTITY = ("private_key", "public_key")


def _writable_sections() -> frozenset[str] | None:
    """The sections `Node.writeConfig` knows; it exits the process on any other."""
    try:
        from meshtastic.node import Node

        return frozenset(re.findall(r'config_name == "(\w+)"', inspect.getsource(Node.writeConfig)))
    except (OSError, TypeError):
        return None


WRITABLE = _writable_sections()


@dataclass
class _Extra:
    """A change that is not a config section: fixed position, canned messages, ringtone."""

    path: str
    wanted: Any
    write: Callable[[Any], Any]
    read: Callable[[Any], Any]  # node -> what the radio holds now


@dataclass
class _Plan:
    sections: dict[str, Message] = field(default_factory=dict)  # section -> its new value
    parents: dict[str, str] = field(default_factory=dict)  # section -> localConfig|moduleConfig
    owner: dict[str, str] = field(default_factory=dict)
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)  # path -> {from, to}
    requested: set[str] = field(default_factory=set)  # paths the caller named
    restored: set[str] = field(default_factory=set)  # sections a restore owns whole
    flags: dict[str, bool] = field(default_factory=dict)  # owner is_licensed / is_unmessagable
    channels: dict[int, Message | None] = field(default_factory=dict)  # index -> new, None = delete
    extras: list[_Extra] = field(default_factory=list)


def _flat(d: dict[str, Any], prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(_flat(value, path))
        else:
            out[path] = value
    return out


def _raw(msg: Message, section: str) -> dict[str, Any]:
    data = json_format.MessageToDict(
        msg, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    return _flat(data, section)


def _section_diff(section: str, old: Message, new: Message) -> dict[str, dict[str, Any]]:
    """Changed paths, found on raw values and shown with sensitive values withheld."""
    raw_old, raw_new = _raw(old, section), _raw(new, section)
    shown_old, shown_new = _flat(to_dict(old), section), _flat(to_dict(new), section)
    return {
        k: {"from": shown_old.get(k), "to": shown_new.get(k)}
        for k in sorted(raw_old.keys() | raw_new.keys())
        if raw_old.get(k) != raw_new.get(k)
    }


def _working_copy(plan: _Plan, node: Any, section: str) -> Message:
    if section not in plan.sections:
        live, parent = protopath.section_container(node, section)
        copy = type(live)()
        copy.CopyFrom(live)
        plan.sections[section] = copy
        plan.parents[section] = parent
    return plan.sections[section]


def _bool(path: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "yes", "on"):
        return True
    if str(value).lower() in ("false", "0", "no", "off"):
        return False
    raise BackendError(f"{path} takes true or false, not {value!r}")


def _plan_channel(plan: _Plan, node: Any, index: int, rest: str, value: Any) -> None:
    from meshtastic.protobuf import channel_pb2
    from meshtastic.util import fromPSK

    channels = node.channels or []
    if index >= len(channels):
        raise BackendError(f"channel.{index}: the radio has {len(channels)} channel slots")
    if rest == "delete":
        if not _bool(f"channel.{index}.delete", value):
            return
        if index == 0:
            raise BackendError("channel.0 is the primary channel and cannot be deleted")
        plan.channels[index] = None
        return
    current = plan.channels.get(index)
    if current is None:
        current = channel_pb2.Channel()
        current.CopyFrom(channels[index])
        current.index = index
        plan.channels[index] = current
    if rest == "role":
        role = str(value).upper()
        roles = list(channel_pb2.Channel.Role.keys())
        if role not in roles:
            raise BackendError(f"channel.{index}.role: choose from {roles}")
        current.role = channel_pb2.Channel.Role.Value(role)
        return
    if current.role == channel_pb2.Channel.Role.DISABLED:
        current.role = channel_pb2.Channel.Role.SECONDARY  # setting a field adds the channel
    segments = rest.removeprefix("settings.").split(".")
    if segments == ["psk"]:
        current.settings.psk = fromPSK(str(value))
        return
    try:
        parent, leaf = protopath.walk_to_field(current.settings, segments)
        setattr(parent, leaf.name, protopath.coerce_scalar(leaf, value))
    except (protopath.PathError, TypeError, ValueError) as exc:
        raise BackendError(f"channel.{index}.{rest}: {exc}") from exc


def _fixed_position(node: Any) -> Any:
    lc = node.localConfig
    return bool(lc.position.fixed_position) if lc.HasField("position") else None


def _plan_extra(plan: _Plan, path: str, value: Any) -> bool:
    if path == "fixed_position":
        if value in (None, False, "", "clear"):
            plan.extras.append(
                _Extra(path, False, lambda n: n.removeFixedPosition(), _fixed_position)
            )
            return True
        if not isinstance(value, dict) or "latitude" not in value or "longitude" not in value:
            raise BackendError(
                "fixed_position takes {latitude, longitude, altitude?}, or null to clear it"
            )
        lat, lon, alt = (
            float(value["latitude"]),
            float(value["longitude"]),
            int(value.get("altitude", 0)),
        )
        plan.extras.append(
            _Extra(path, True, lambda n: n.setFixedPosition(lat, lon, alt), _fixed_position)
        )
        return True
    if path == "canned_messages":
        text = "|".join(value) if isinstance(value, list) else str(value)
        plan.extras.append(
            _Extra(
                path, text, lambda n: n.set_canned_message(text), lambda n: n.get_canned_message()
            )
        )
        return True
    if path == "ringtone":
        tune = str(value)
        plan.extras.append(
            _Extra(path, tune, lambda n: n.set_ringtone(tune), lambda n: n.get_ringtone())
        )
        return True
    return False


def _plan_changes(plan: _Plan, node: Any, changes: dict[str, Any]) -> None:
    for path, value in changes.items():
        if path in OWNER_KEYS:
            if not str(value).strip():
                raise BackendError(f"{path} cannot be blank")
            plan.owner[path.split(".", 1)[1]] = str(value)
            continue
        if path in OWNER_FLAGS:
            plan.flags[path.split(".", 1)[1]] = _bool(path, value)
            continue
        channel = _CHANNEL.match(path)
        if channel:
            _plan_channel(plan, node, int(channel.group(1)), channel.group(2), value)
            continue
        if _plan_extra(plan, path, value):
            continue
        segments = [s for s in path.split(".") if s]
        plan.requested.add(".".join(segments))
        if len(segments) < 2:
            raise BackendError(f"{path!r}: name a field, such as lora.region")
        try:
            copy = _working_copy(plan, node, segments[0])
            parent, leaf = protopath.walk_to_field(copy, segments[1:])
            if leaf.is_repeated:
                raise BackendError(f"{path!r} is a repeated field; configure sets scalars only")
            setattr(parent, leaf.name, protopath.coerce_scalar(leaf, value))
        except (protopath.PathError, TypeError, ValueError) as exc:
            raise BackendError(str(exc)) from exc


def _plan_restore(plan: _Plan, node: Any, snapshot: dict[str, Any]) -> None:
    sources = (
        ("localConfig", localonly_pb2.LocalConfig(), node.localConfig),
        ("moduleConfig", localonly_pb2.LocalModuleConfig(), node.moduleConfig),
    )
    for key, saved, live in sources:
        json_format.ParseDict(snapshot.get(key) or {}, saved, ignore_unknown_fields=True)
        for desc in saved.DESCRIPTOR.fields:
            if desc.message_type is None or not saved.HasField(desc.name):
                continue
            copy = _working_copy(plan, node, desc.name)
            copy.CopyFrom(getattr(saved, desc.name))
            plan.restored.add(desc.name)
            if desc.name == "security":
                # A snapshot never moves a radio's identity, even one taken from another radio.
                for key_field in _IDENTITY:
                    setattr(copy, key_field, getattr(live.security, key_field))


def _owner_now(iface: Any, num: int | None = None) -> dict[str, Any]:
    if num is None:
        num = getattr(iface.myInfo, "my_node_num", None)
    user = ((iface.nodesByNum or {}).get(num) or {}).get("user") or {}
    return {
        "long_name": user.get("longName"),
        "short_name": user.get("shortName"),
        "is_licensed": bool(user.get("isLicensed", False)),
        "is_unmessagable": bool(user.get("isUnmessagable", False)),
    }


def _channel_dict(ch: Message | None) -> dict[str, Any] | None:
    return None if ch is None else to_dict(ch)


def _finish_plan(plan: _Plan, iface: Any, node: Any = None, num: int | None = None) -> None:
    node = node or iface.localNode
    for section in list(plan.sections):
        live, _ = protopath.section_container(node, section)
        diff = _section_diff(section, live, plan.sections[section])
        if not diff:
            del plan.sections[section]  # already so; writing it would only restart the radio
            continue
        if WRITABLE is not None and section not in WRITABLE:
            raise BackendError(
                f"meshtastic-python cannot write the {section} section; leave it out of this call"
            )
        plan.changes.update(diff)
    owner = _owner_now(iface, num)
    for bucket in (plan.owner, plan.flags):
        for key in list(bucket):
            if owner.get(key) == bucket[key]:
                del bucket[key]
            else:
                plan.changes[f"owner.{key}"] = {"from": owner.get(key), "to": bucket[key]}
    channels = (node.channels or []) if plan.channels else []
    for index, new in list(plan.channels.items()):
        old = channels[index]
        if new is not None and new.SerializeToString() == old.SerializeToString():
            del plan.channels[index]
            continue
        plan.changes[f"channel.{index}"] = {
            "from": _channel_dict(old),
            "to": "deleted" if new is None else _channel_dict(new),
        }
    for extra in list(plan.extras):
        if extra.path in ("canned_messages", "ringtone"):
            # Reading these asks the radio, so a write always goes out; read-back verifies it.
            plan.changes[extra.path] = {"from": None, "to": extra.wanted}
            continue
        now = extra.read(node)
        plan.changes[extra.path] = {"from": now, "to": extra.wanted}


def _apply(plan: _Plan, iface: Any, node: Any = None, num: int | None = None) -> bool:
    node = node or iface.localNode
    writes: list[Callable[[], Any]] = []
    for section, value in plan.sections.items():
        container, _ = protopath.section_container(node, section)

        def write_section(
            section: str = section, container: Any = container, value: Any = value
        ) -> None:
            container.CopyFrom(value)
            node.writeConfig(section)

        writes.append(write_section)
    if plan.owner or plan.flags:
        current = _owner_now(iface, num)
        merged = {**current, **plan.owner, **plan.flags}
        writes.append(
            lambda: node.setOwner(
                long_name=merged["long_name"],
                short_name=merged["short_name"],
                is_licensed=merged["is_licensed"],
                is_unmessagable=merged["is_unmessagable"],
            )
        )
    deletes = sorted((i for i, ch in plan.channels.items() if ch is None), reverse=True)
    for index, new in sorted(plan.channels.items()):
        if new is None:
            continue

        def write_channel(index: int = index, new: Message = new) -> None:
            node.channels[index].CopyFrom(new)
            node.writeChannel(index)

        writes.append(write_channel)
    for index in deletes:  # highest first, so a delete never shifts one still to come
        writes.append(functools.partial(node.deleteChannel, index))
    for extra in plan.extras:
        writes.append(functools.partial(extra.write, node))
    transacted = len(writes) > 1
    if transacted:
        node.beginSettingsTransaction()
        time.sleep(PACE_S)
    for send in writes:
        send()
        if transacted:
            time.sleep(PACE_S)
    if transacted:
        node.commitSettingsTransaction()
    return transacted


def _read_back(plan: _Plan, iface: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(mismatches, side_effects)`: requested values that did not stick, and other fields in
    the written sections that the firmware changed on its own (a region change caps
    `tx_power`, for one)."""
    mismatches: dict[str, dict[str, Any]] = {}
    side_effects: dict[str, dict[str, Any]] = {}
    node = iface.localNode
    for section, wanted in plan.sections.items():
        live, _ = protopath.section_container(node, section)
        for path, values in _section_diff(section, wanted, live).items():
            if section in plan.restored or path in plan.requested:
                mismatches[path] = {"wanted": values["from"], "read_back": values["to"]}
            else:
                side_effects[path] = values
    owner = _owner_now(iface)
    for key, wanted in {**plan.owner, **plan.flags}.items():
        if owner.get(key) != wanted:
            mismatches[f"owner.{key}"] = {"wanted": wanted, "read_back": owner.get(key)}
    channels = (node.channels or []) if plan.channels else []
    for index, new in plan.channels.items():
        live = channels[index] if index < len(channels) else None
        if new is None:
            continue  # a delete shifts the later slots up; nothing stays to compare
        same = live is not None and live.SerializeToString() == new.SerializeToString()
        if not same:
            mismatches[f"channel.{index}"] = {
                "wanted": _channel_dict(new),
                "read_back": _channel_dict(live),
            }
    for extra in plan.extras:
        held = extra.read(node)
        if held != extra.wanted:
            mismatches[extra.path] = {"wanted": extra.wanted, "read_back": held}
    return mismatches, side_effects


def _uptime(iface: Any, timeout_s: float = 5.0) -> tuple[float, float] | None:
    """`(uptime_s, monotonic time it was read)` from the radio's device telemetry, or None."""
    done = threading.Event()
    box: dict[str, Any] = {}

    def on_response(packet: dict[str, Any]) -> None:
        metrics = ((packet.get("decoded") or {}).get("telemetry") or {}).get("deviceMetrics")
        box["uptime"] = (metrics or {}).get("uptimeSeconds")
        done.set()

    request = telemetry_pb2.Telemetry()
    request.device_metrics.SetInParent()
    iface.sendData(
        request,
        destinationId=iface.myInfo.my_node_num,
        portNum=portnums_pb2.PortNum.TELEMETRY_APP,
        wantResponse=True,
        onResponse=on_response,
    )
    if not done.wait(timeout_s) or box.get("uptime") is None:
        return None
    return float(box["uptime"]), time.monotonic()


def _restarted(
    before: tuple[float, float] | None, after: tuple[float, float] | None
) -> bool | None:
    if before is None or after is None:
        return None
    elapsed = after[1] - before[1]
    return after[0] + UPTIME_SLACK_S < before[0] + elapsed


def configure(
    target: str | None,
    changes: dict[str, Any] | None,
    restore: dict[str, Any] | None,
    dry_run: bool,
    timeout_s: float,
    save_snapshot: Callable[[str, dict[str, Any]], dict[str, Any]],
    dest: str | int | None = None,
) -> dict[str, Any]:
    if dest is not None:
        if restore is not None:
            raise BackendError("restore applies to the attached radio; it cannot go to `dest`")
        return _configure_remote(target, changes or {}, dry_run, timeout_s, dest)
    deadline = time.monotonic() + timeout_s
    plan = _Plan()
    with connection.connect(port=target, timeout_s=min(timeout_s, 10.0)) as iface:
        port = getattr(iface, "devPath", None) or target
        if restore is not None:
            _plan_restore(plan, iface.localNode, restore)
        _plan_changes(plan, iface.localNode, changes or {})
        _finish_plan(plan, iface)
        out: dict[str, Any] = {"target": port, "dry_run": dry_run, "changes": plan.changes}
        if dry_run or not plan.changes:
            out.update(verified=None if dry_run else True, restarted=False if not dry_run else None)
            return out
        out["snapshot"] = save_snapshot(
            str(port),
            {
                "localConfig": protopath.message_to_dict(iface.localNode.localConfig),
                "moduleConfig": protopath.message_to_dict(iface.localNode.moduleConfig),
            },
        )
        before = _uptime(iface)
        out["transaction"] = _apply(plan, iface)
        time.sleep(2.5)  # let the radio finish its flash write before the port closes
    # Wait out the firmware's restart timer, then read back over a fresh connection.
    time.sleep(max(0.0, min(REBOOT_DELAY_S + BOOT_S - 2.5, deadline - time.monotonic())))
    last_error = ""
    while True:
        try:
            with connection.connect(port=port, timeout_s=8.0) as iface:
                after = _uptime(iface)
                mismatches, side_effects = _read_back(plan, iface)
            break
        except connection.not_answering() as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                out.update(verified=False, restarted=None)
                out["errors"] = {"read_back": f"the radio did not answer again: {last_error}"}
                return out
            time.sleep(2.0)
    out["verified"] = not mismatches
    if mismatches:
        out["mismatches"] = mismatches
    if side_effects:
        out["side_effects"] = side_effects
    out["restarted"] = _restarted(before, after)
    out["uptime_s"] = {"before": before and before[0], "after": after and after[0]}
    return out


def _request_sections(
    node: Any, sections: set[str], iface: Any, dest: str | int, acks: Any
) -> None:
    """Ask `dest` for each section; the library waits for each answer."""
    from .python_admin import refusal

    for section in sorted(sections):
        parent = localonly_pb2.LocalConfig if section in _LOCAL else localonly_pb2.LocalModuleConfig
        field = parent.DESCRIPTOR.fields_by_name.get(section)
        if field is None:
            raise BackendError(f"{section!r} is not a config section")
        iface._acknowledgment.reset()
        try:
            node.requestConfig(field)
        except Exception as exc:  # the library's MeshInterfaceError on a timeout
            raise BackendError(f"{dest} did not send its {section} config: {exc}") from None
        if acks.reason:
            raise refusal(dest, acks.reason)


_LOCAL = frozenset(f.name for f in localonly_pb2.LocalConfig.DESCRIPTOR.fields)


def _configure_remote(
    target: str | None,
    changes: dict[str, Any],
    dry_run: bool,
    timeout_s: float,
    dest: str | int,
) -> dict[str, Any]:
    """Write config to another node over PKI admin, then ask it for each section again.

    No snapshot is taken: the attached radio's snapshot would not describe the remote node.
    """
    from .python_admin import remote_node, wait_ack

    sections = {p.split(".", 1)[0] for p in changes if "." in p}
    sections -= {"owner", "channel"}
    plan = _Plan()
    with connection.connect(port=target, timeout_s=min(timeout_s, 10.0)) as iface:
        port = getattr(iface, "devPath", None) or target
        node, acks = remote_node(iface, dest, timeout_s)
        num = node.nodeNum
        _request_sections(node, sections, iface, dest, acks)
        if any(p.startswith("channel.") for p in changes):
            node.requestChannels()
            if not node.waitForConfig():
                raise BackendError(f"{dest} sent no channel list within {timeout_s:g} s")
        _plan_changes(plan, node, changes)
        _finish_plan(plan, iface, node, num)
        out: dict[str, Any] = {
            "target": port,
            "dest": str(dest),
            "dry_run": dry_run,
            "changes": plan.changes,
        }
        if dry_run or not plan.changes:
            out.update(verified=None if dry_run else True, restarted=None)
            return out
        iface._acknowledgment.reset()
        out["transaction"] = _apply(plan, iface, node, num)
        wait_ack(iface, dest, acks)
        # Ask again for what was written; a node that restarts answers once it is back.
        time.sleep(REBOOT_DELAY_S + BOOT_S)
        _request_sections(node, set(plan.sections), iface, dest, acks)
        mismatches: dict[str, Any] = {}
        for section, wanted in plan.sections.items():
            live, _ = protopath.section_container(node, section)
            for path, values in _section_diff(section, wanted, live).items():
                if path in plan.requested:
                    mismatches[path] = {"wanted": values["from"], "read_back": values["to"]}
    out["verified"] = not mismatches
    if mismatches:
        out["mismatches"] = mismatches
    if plan.owner or plan.flags or plan.channels or plan.extras:
        out["unverified"] = sorted(
            k for k in plan.changes if k.split(".", 1)[0] not in plan.sections
        )
    out["restarted"] = None
    return out
