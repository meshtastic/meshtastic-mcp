# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Radio-level actions over meshtastic-python: restart, power off, reset, and the node DB.

An action that restarts the radio is followed, when asked, by a wait until it answers again,
and the uptime it then reports shows the restart happened.
"""

from __future__ import annotations

import time
from typing import Any

from .. import connection
from . import BackendError
from .python_configure import BOOT_S, REBOOT_DELAY_S, _uptime

RESTARTS = frozenset({"reboot", "factory_reset", "factory_reset_full", "reset_nodedb"})
NODE_ACTIONS = frozenset({"favorite", "unfavorite", "ignore", "unignore", "remove_node"})
ACTIONS = RESTARTS | NODE_ACTIONS | {"shutdown", "set_time", "add_contact", "enter_uf2"}
# A remote node answers an admin message it refuses with one of these routing errors.
_REFUSALS = {
    "ADMIN_PUBLIC_KEY_UNAUTHORIZED": "this radio's public key is not in its security.admin_key",
    "NOT_AUTHORIZED": "it does not accept admin from this radio",
    "PKI_UNKNOWN_PUBKEY": "it has no public key for this radio yet; let NodeInfo reach it first",
    "ADMIN_BAD_SESSION_KEY": "the admin session expired; retry",
    "NO_CHANNEL": "no admin channel or PKI route reaches it",
}


class _Acks:
    """Collects a remote node's ACK or NAK; the library's own handler only prints it."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.acked = False

        def on_ack_nak(packet: dict[str, Any]) -> None:
            reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason")
            if reason and reason != "NONE":
                self.reason = reason
            else:
                self.acked = True

        self.on_ack_nak = on_ack_nak


def refusal(dest: str | int, reason: str) -> BackendError:
    why = _REFUSALS.get(reason, "it refused the request")
    return BackendError(f"{dest} answered {reason}: {why}")


def remote_node(iface: Any, dest: str | int, timeout_s: float) -> tuple[Any, _Acks]:
    """A Node for `dest` that reports ACKs to us, with an admin session key in hand.

    Admin to another node goes PKI-encrypted; the node must list this radio's public key in its
    `security.admin_key`, and refuses otherwise.
    """
    from meshtastic.node import Node

    num = int(dest[1:], 16) if isinstance(dest, str) and dest.startswith("!") else dest
    node = Node(iface, num, noProto=False, timeout=int(timeout_s))
    acks = _Acks()
    node.onAckNak = acks.on_ack_nak
    node.onResponseRequestSettings = _session_only(node, acks)
    try:
        node.ensureSessionKey()
    except Exception as exc:  # the library raises its own MeshInterfaceError on a timeout
        raise BackendError(
            f"{dest} gave no admin session key within {timeout_s:g} s: {exc}"
        ) from None
    if acks.reason:
        raise refusal(dest, acks.reason)
    return node, acks


def _session_only(node: Any, acks: _Acks) -> Any:
    original = node.onResponseRequestSettings

    def on_settings(packet: dict[str, Any]) -> None:
        reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason")
        if reason and reason != "NONE":
            acks.reason = reason
            node.iface._acknowledgment.receivedNak = True
            return
        original(packet)

    return on_settings


def wait_ack(iface: Any, dest: str | int, acks: _Acks) -> None:
    try:
        iface.waitForAckNak()
    except Exception as exc:
        raise BackendError(f"{dest} did not acknowledge: {exc}") from None
    if acks.reason:
        raise refusal(dest, acks.reason)


def _node_id(node_id: str | int | None) -> int | str:
    if node_id is None or node_id == "":
        raise BackendError("this action names a node: pass `node_id`")
    if isinstance(node_id, str) and node_id.isdigit():
        return int(node_id)
    return node_id


def _run(node: Any, action: str, node_id: str | int | None, seconds: int, value: Any) -> None:
    if action == "reboot":
        node.reboot(secs=seconds)
    elif action == "shutdown":
        node.shutdown(secs=seconds)
    elif action == "factory_reset":
        node.factoryReset(full=False)
    elif action == "factory_reset_full":
        node.factoryReset(full=True)
    elif action == "reset_nodedb":
        node.resetNodeDb()
    elif action == "set_time":
        node.setTime(int(value or 0))
    elif action == "add_contact":
        if not value or "/v/#" not in str(value):
            raise BackendError("add_contact takes a contact URL (https://meshtastic.org/v/#...)")
        node.addContactURL(str(value))
    elif action == "enter_uf2":
        node.enterDFUMode()
    else:
        nid = _node_id(node_id)
        {
            "favorite": node.setFavorite,
            "unfavorite": node.removeFavorite,
            "ignore": node.setIgnored,
            "unignore": node.removeIgnored,
            "remove_node": node.removeNode,
        }[action](nid)


def admin(
    target: str | None,
    action: str,
    node_id: str | int | None,
    seconds: int,
    wait_back: bool,
    timeout_s: float,
    dest: str | int | None = None,
    value: Any = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise BackendError(f"action={action!r}: choose from {sorted(ACTIONS)}")
    if dest is not None:
        return _admin_remote(target, action, node_id, seconds, timeout_s, dest, value)
    deadline = time.monotonic() + timeout_s
    with connection.connect(port=target, timeout_s=10.0) as iface:
        port = getattr(iface, "devPath", None) or target
        node = iface.localNode
        before = _uptime(iface) if action in RESTARTS and wait_back else None
        _run(node, action, node_id, seconds, value)
        time.sleep(2.5)  # the request must leave before the port closes
    out: dict[str, Any] = {"target": port, "action": action, "ok": True}
    if node_id is not None and action in NODE_ACTIONS:
        out["node_id"] = node_id
    if action == "set_time":
        out["time"] = int(value or 0) or int(time.time())
    if action not in RESTARTS or not wait_back:
        return out
    # A reset restarts on the firmware's own timer, not after `seconds`.
    delay = seconds if action == "reboot" else REBOOT_DELAY_S
    time.sleep(max(0.0, min(delay + BOOT_S, deadline - time.monotonic())))
    last = ""
    while True:
        try:
            with connection.connect(port=port, timeout_s=8.0) as iface:
                after = _uptime(iface)
            break
        except connection.not_answering() as exc:
            last = str(exc)
            if time.monotonic() >= deadline:
                out.update(back=False, error=f"the radio did not answer again: {last}")
                return out
            time.sleep(2.0)
    out["back"] = True
    if before is not None and after is not None:
        out["restarted"] = after[0] + 5.0 < before[0] + (after[1] - before[1])
        out["uptime_s"] = after[0]
    return out


def _admin_remote(
    target: str | None,
    action: str,
    node_id: str | int | None,
    seconds: int,
    timeout_s: float,
    dest: str | int,
    value: Any,
) -> dict[str, Any]:
    if action == "enter_uf2":
        raise BackendError("enter_uf2 needs the board on USB; it cannot go to another node")
    with connection.connect(port=target, timeout_s=10.0) as iface:
        port = getattr(iface, "devPath", None) or target
        node, acks = remote_node(iface, dest, timeout_s)
        iface._acknowledgment.reset()
        _run(node, action, node_id, seconds, value)
        wait_ack(iface, dest, acks)
    out: dict[str, Any] = {"target": port, "dest": str(dest), "action": action, "ok": True}
    if node_id is not None and action in NODE_ACTIONS:
        out["node_id"] = node_id
    if action in RESTARTS:
        out["back"] = None  # a remote restart is not watched; inspect it over the mesh later
    return out
