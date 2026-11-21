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
ACTIONS = RESTARTS | NODE_ACTIONS | {"shutdown"}


def _node_id(node_id: str | int | None) -> int | str:
    if node_id is None or node_id == "":
        raise BackendError("this action names a node: pass `node_id`")
    if isinstance(node_id, str) and node_id.isdigit():
        return int(node_id)
    return node_id


def admin(
    target: str | None,
    action: str,
    node_id: str | int | None,
    seconds: int,
    wait_back: bool,
    timeout_s: float,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise BackendError(f"action={action!r}: choose from {sorted(ACTIONS)}")
    deadline = time.monotonic() + timeout_s
    with connection.connect(port=target, timeout_s=10.0) as iface:
        port = getattr(iface, "devPath", None) or target
        node = iface.localNode
        before = _uptime(iface) if action in RESTARTS and wait_back else None
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
        else:
            nid = _node_id(node_id)
            {
                "favorite": node.setFavorite,
                "unfavorite": node.removeFavorite,
                "ignore": node.setIgnored,
                "unignore": node.removeIgnored,
                "remove_node": node.removeNode,
            }[action](nid)
        time.sleep(2.5)  # the request must leave before the port closes
    out: dict[str, Any] = {"target": port, "action": action, "ok": True}
    if node_id is not None and action in NODE_ACTIONS:
        out["node_id"] = node_id
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
