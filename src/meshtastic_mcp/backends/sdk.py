# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The Kotlin SDK backend, through its CLI: BLE as well as TCP and serial."""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from .. import sdk_cli
from . import BackendError


def _get(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return None


def _node(raw: dict[str, Any], detail: str) -> dict[str, Any]:
    node = raw.get("node", raw)
    if detail == "full":
        return node
    user = node.get("user") or {}
    metrics = _get(node, "device_metrics", "deviceMetrics") or {}
    num = node.get("num")
    return {
        "node_num": num,
        "node_id": f"!{num & 0xFFFFFFFF:08x}" if isinstance(num, int) else None,
        "long_name": _get(user, "long_name", "longName"),
        "short_name": _get(user, "short_name", "shortName"),
        "hw_model": _get(user, "hw_model", "hwModel"),
        "role": user.get("role"),
        "hops_away": _get(node, "hops_away", "hopsAway"),
        "snr": node.get("snr"),
        "last_heard": _get(node, "last_heard", "lastHeard"),
        "battery_level": _get(metrics, "battery_level", "batteryLevel"),
        "is_favorite": bool(_get(node, "is_favorite", "isFavorite")),
    }


def _checked(result: dict[str, Any], what: str) -> dict[str, Any]:
    if not result.get("ok"):
        error = result.get("error") or {}
        message = error.get("message") or result.get("stderr") or f"exit {result.get('exit')}"
        raise BackendError(f"the SDK CLI could not {what}: {message}")
    return result


class SdkBackend:
    name = "sdk"
    parts = frozenset({"summary", "nodes"})

    def read(
        self,
        target: str | None,
        parts: Collection[str],
        sections: Collection[str] | None,
        detail: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        if not target:
            raise BackendError(
                "the SDK backend needs a target: ble:<name or address>, tcp: or serial:"
            )
        unsupported = sorted(set(parts) - self.parts)
        if unsupported:
            raise BackendError(
                f"the SDK backend reads only {sorted(self.parts)}, not {unsupported}"
            )
        if not sdk_cli.available():
            raise BackendError(
                f"{target} needs the SDK backend, and its CLI is not installed; "
                f"set {sdk_cli.CLI_ENV} or {sdk_cli.ROOT_ENV}"
            )
        timeout_ms = int(timeout_s * 1000)
        out: dict[str, Any] = {"target": target}
        if "summary" in parts:
            res = _checked(sdk_cli.device_info(target, timeout_ms=timeout_ms), "read the radio")
            info = res.get("info") or {}
            own = _node(info.get("ownNode") or {}, "brief")
            out["summary"] = {**own, "num_nodes": info.get("nodeCount")}
            if detail == "full":
                out["summary"]["own_node"] = info.get("ownNode")
        if "nodes" in parts:
            res = _checked(sdk_cli.list_nodes(target, timeout_ms=timeout_ms), "list nodes")
            out["nodes"] = [_node(n, detail) for n in res.get("nodes") or []]
        return out

    def configure(
        self,
        target: str | None,
        changes: dict[str, Any] | None,
        restore: dict[str, Any] | None,
        dry_run: bool,
        timeout_s: float,
        save_snapshot: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        raise BackendError("the SDK backend does not write config yet; use a serial or TCP target")

    def admin(
        self,
        target: str | None,
        action: str,
        node_id: str | int | None,
        seconds: int,
        wait_back: bool,
        timeout_s: float,
    ) -> dict[str, Any]:
        raise BackendError("the SDK backend has no admin actions yet; use a serial or TCP target")

    def send(
        self,
        target: str | None,
        to: str | int | None,
        payload: dict[str, Any],
        channel: int,
        want_ack: bool,
        hop_limit: int | None,
        wait_s: float,
    ) -> dict[str, Any]:
        kind, value = next(iter(payload.items()))
        if kind != "text":
            raise BackendError("the SDK backend sends text only; use a serial or TCP target")
        if not target or not sdk_cli.available():
            raise BackendError(f"sending over {target} needs the SDK CLI; set {sdk_cli.CLI_ENV}")
        dest = "BROADCAST" if to in (None, "", "^all") else str(to)
        if dest.startswith("!"):
            dest = str(int(dest[1:], 16))
        res = sdk_cli.send_text(
            target, str(value), to=dest, channel=channel, await_ms=int(wait_s * 1000)
        )
        error = res.get("error") or {}
        results = [i for i in res["by_type"].get("info", []) if i.get("kind") == "send-result"]
        queued = [i for i in res["by_type"].get("info", []) if i.get("kind") == "send-queued"]
        if error.get("code") == "timeout":
            delivery = "no_ack"
        elif results and results[-1].get("outcome") == "success":
            delivery = "delivered"
        elif results:
            delivery = f"failed:{results[-1].get('reason')}"
        elif not res.get("ok"):
            raise BackendError(f"the SDK CLI could not send: {error.get('message') or res}")
        else:
            delivery = None
        return {
            "target": target,
            "packet_id": queued[0].get("messageId") if queued else None,
            "destination": dest,
            "kind": "text",
            "delivery": delivery,
        }
