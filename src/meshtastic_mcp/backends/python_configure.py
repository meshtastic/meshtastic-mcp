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

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import Message
from meshtastic.protobuf import localonly_pb2, portnums_pb2, telemetry_pb2

from .. import admin, connection
from . import BackendError
from .protodict import to_dict

PACE_S = 0.25  # between writes inside a transaction
REBOOT_DELAY_S = 7.0  # firmware DEFAULT_REBOOT_SECONDS after a save that restarts
BOOT_S = 3.0  # time for the radio to come back far enough to answer
UPTIME_SLACK_S = 5.0  # clock and reply jitter allowed before a lower uptime counts as a restart
OWNER_KEYS = ("owner.long_name", "owner.short_name")
_IDENTITY = ("private_key", "public_key")


@dataclass
class _Plan:
    sections: dict[str, Message] = field(default_factory=dict)  # section -> its new value
    parents: dict[str, str] = field(default_factory=dict)  # section -> localConfig|moduleConfig
    owner: dict[str, str] = field(default_factory=dict)
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)  # path -> {from, to}
    requested: set[str] = field(default_factory=set)  # paths the caller named
    restored: set[str] = field(default_factory=set)  # sections a restore owns whole


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
        live, parent = admin._section_container(node, section)
        copy = type(live)()
        copy.CopyFrom(live)
        plan.sections[section] = copy
        plan.parents[section] = parent
    return plan.sections[section]


def _plan_changes(plan: _Plan, node: Any, changes: dict[str, Any]) -> None:
    for path, value in changes.items():
        if path in OWNER_KEYS:
            plan.owner[path.split(".", 1)[1]] = str(value)
            continue
        segments = [s for s in path.split(".") if s]
        plan.requested.add(".".join(segments))
        if len(segments) < 2:
            raise BackendError(f"{path!r}: name a field, such as lora.region")
        try:
            copy = _working_copy(plan, node, segments[0])
            parent, leaf = admin._walk_to_field(copy, segments[1:])
            if leaf.is_repeated:
                raise BackendError(f"{path!r} is a repeated field; configure sets scalars only")
            setattr(parent, leaf.name, admin._coerce_scalar(leaf, value))
        except (admin.AdminError, TypeError, ValueError) as exc:
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


def _owner_now(iface: Any) -> dict[str, Any]:
    num = getattr(iface.myInfo, "my_node_num", None)
    user = ((iface.nodesByNum or {}).get(num) or {}).get("user") or {}
    return {"long_name": user.get("longName"), "short_name": user.get("shortName")}


def _finish_plan(plan: _Plan, iface: Any) -> None:
    node = iface.localNode
    for section in list(plan.sections):
        live, _ = admin._section_container(node, section)
        diff = _section_diff(section, live, plan.sections[section])
        if not diff:
            del plan.sections[section]  # already so; writing it would only restart the radio
            continue
        plan.changes.update(diff)
    owner = _owner_now(iface)
    for key in list(plan.owner):
        if owner.get(key) == plan.owner[key]:
            del plan.owner[key]
        else:
            plan.changes[f"owner.{key}"] = {"from": owner.get(key), "to": plan.owner[key]}


def _apply(plan: _Plan, iface: Any) -> bool:
    node = iface.localNode
    writes: list[Callable[[], Any]] = []
    for section, value in plan.sections.items():
        container, _ = admin._section_container(node, section)

        def write_section(
            section: str = section, container: Any = container, value: Any = value
        ) -> None:
            container.CopyFrom(value)
            node.writeConfig(section)

        writes.append(write_section)
    if plan.owner:
        current = _owner_now(iface)
        long_name = plan.owner.get("long_name", current["long_name"])
        short_name = plan.owner.get("short_name", current["short_name"])
        writes.append(lambda: node.setOwner(long_name=long_name, short_name=short_name))
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
        live, _ = admin._section_container(node, section)
        for path, values in _section_diff(section, wanted, live).items():
            if section in plan.restored or path in plan.requested:
                mismatches[path] = {"wanted": values["from"], "read_back": values["to"]}
            else:
                side_effects[path] = values
    owner = _owner_now(iface)
    for key, wanted in plan.owner.items():
        if owner.get(key) != wanted:
            mismatches[f"owner.{key}"] = {"wanted": wanted, "read_back": owner.get(key)}
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
) -> dict[str, Any]:
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
                "localConfig": admin._message_to_dict(iface.localNode.localConfig),
                "moduleConfig": admin._message_to_dict(iface.localNode.moduleConfig),
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
        except (connection.ConnectionError, OSError, TimeoutError) as exc:
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
