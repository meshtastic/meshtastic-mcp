# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Sending over meshtastic-python: text, position, waypoint, and requests that wait for a reply.

A routing ACK can come from the destination, which means it was delivered, or from our own radio
when it hears a neighbor relay the packet, which means only that it left. `ack_from` says which.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

from .. import connection
from . import BackendError

# A broadcast waits ~4 s for channel politeness, then airtime, before the port may close.
LINGER_S = 8.0


def _dest(to: str | int | None) -> str | int:
    if to is None or to in ("", "^all", "broadcast", "BROADCAST"):
        return "^all"
    if isinstance(to, str) and to.isdigit():
        return int(to)
    return to


def _num(dest: str | int) -> int | None:
    if isinstance(dest, int):
        return dest
    if dest.startswith("!"):
        return int(dest[1:], 16)
    return None


def _check_dest(iface: Any, dest: str | int) -> None:
    """meshtastic-python exits the process for a name it cannot find, so refuse it first."""
    if isinstance(dest, int) or dest == "^all" or dest.startswith("!"):
        return
    if dest in (getattr(iface, "nodes", None) or {}):
        return
    if len(dest) == 8 and all(c in "0123456789abcdefABCDEF" for c in dest):
        return
    raise BackendError(
        f"no node {dest!r} in the radio's node DB; pass its node id (!aabbccdd) or number"
    )


class _Reply:
    """The first answer to a packet. `on_response` is a plain function because meshtastic-python
    reads the callback's `__name__`."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.packet: dict[str, Any] | None = None

        def on_response(packet: dict[str, Any]) -> None:
            self.packet = packet
            self.done.set()

        self.on_response = on_response


def _clean(reply: Any) -> Any:
    return {k: v for k, v in reply.items() if k != "raw"} if isinstance(reply, dict) else reply


def _ack(reply: _Reply, my_num: int, dest_num: int | None) -> dict[str, Any]:
    if reply.packet is None:
        return {"delivery": "no_ack"}
    routing = (reply.packet.get("decoded") or {}).get("routing") or {}
    reason = routing.get("errorReason", "NONE")
    sender = reply.packet.get("from")
    out: dict[str, Any] = {"ack_from": f"!{sender:08x}" if isinstance(sender, int) else sender}
    if reason != "NONE":
        out["delivery"] = f"nak:{reason}"
    elif sender == dest_num:
        out["delivery"] = "delivered"
    elif sender == my_num:
        out["delivery"] = "relayed"  # our radio heard a neighbor rebroadcast it
    else:
        out["delivery"] = "acked"
    return out


TELEMETRY = {
    "device": "device_metrics",
    "environment": "environment_metrics",
    "air_quality": "air_quality_metrics",
    "power": "power_metrics",
    "local_stats": "local_stats",
    "health": "health_metrics",
    "host": "host_metrics",
}
REQUESTS = (
    "traceroute",
    "position",
    "neighbor_info",
    "node_info",
    *(f"telemetry:{v}" for v in TELEMETRY),
)
_ANSWERS = ("traceroute", "telemetry", "position", "neighborinfo", "user")


def _answer(decoded: dict[str, Any]) -> Any:
    for key in _ANSWERS:
        if key in decoded:
            return decoded[key]
    return None


def _hops(iface: Any, hop_limit: int | None) -> int | None:
    """The caller's hop limit, else the radio's own; firmware answers a request only within it."""
    if hop_limit:
        return hop_limit
    node = getattr(iface, "localNode", None)
    lora = getattr(getattr(node, "localConfig", None), "lora", None)
    own = int(getattr(lora, "hop_limit", 0) or 0)
    return own or None


def _request(
    iface: Any, dest: str | int, what: str, reply: _Reply, channel: int, hop_limit: int | None
) -> Any:
    """Ask `dest` for something. Firmware routes a request only with want_ack set and a hop limit,
    and a position request without want_ack keeps a hop limit of zero."""
    common: dict[str, Any] = {
        "destinationId": dest,
        "wantAck": True,
        "wantResponse": True,
        "onResponse": reply.on_response,
        "channelIndex": channel,
        "hopLimit": _hops(iface, hop_limit),
    }
    if what == "traceroute":
        return iface.sendData(
            mesh_pb2.RouteDiscovery(), portNum=portnums_pb2.PortNum.TRACEROUTE_APP, **common
        )
    if what == "telemetry" or what.startswith("telemetry:"):
        variant = TELEMETRY.get(what.partition(":")[2] or "device")
        if variant is None:
            raise BackendError(f"telemetry variant {what!r}: choose from {sorted(TELEMETRY)}")
        request = telemetry_pb2.Telemetry()
        getattr(request, variant).SetInParent()  # firmware ignores a request naming no variant
        return iface.sendData(request, portNum=portnums_pb2.PortNum.TELEMETRY_APP, **common)
    if what == "position":
        return iface.sendData(
            mesh_pb2.Position(), portNum=portnums_pb2.PortNum.POSITION_APP, **common
        )
    if what == "neighbor_info":
        return iface.sendData(
            mesh_pb2.NeighborInfo(), portNum=portnums_pb2.PortNum.NEIGHBORINFO_APP, **common
        )
    if what == "node_info":
        user = ((iface.nodesByNum or {}).get(iface.myInfo.my_node_num) or {}).get("user") or {}
        ours = mesh_pb2.User(
            id=str(user.get("id", "")),
            long_name=str(user.get("longName", "")),
            short_name=str(user.get("shortName", "")),
        )
        return iface.sendData(ours, portNum=portnums_pb2.PortNum.NODEINFO_APP, **common)
    raise BackendError(f"request={what!r}: choose from {list(REQUESTS)}")


def send(
    target: str | None,
    to: str | int | None,
    payload: dict[str, Any],
    channel: int,
    want_ack: bool,
    hop_limit: int | None,
    wait_s: float,
    linger_s: float | None = None,
) -> dict[str, Any]:
    kind, value = next(iter(payload.items()))
    dest = _dest(to)
    broadcast = dest == "^all"
    if kind == "request" and broadcast:
        raise BackendError("a request goes to one node; pass `to`")
    if want_ack and broadcast:
        raise BackendError("want_ack needs a direct `to`; a broadcast is never acknowledged")
    if want_ack and kind in ("position", "waypoint"):
        raise BackendError("want_ack is for text; position and waypoint are sent without one")
    started = time.monotonic()
    with connection.connect(port=target, timeout_s=10.0) as iface:
        my_num = iface.myInfo.my_node_num
        _check_dest(iface, dest)
        dest_num = _num(dest)
        reply = _Reply()
        common: dict[str, Any] = {"channelIndex": channel, "hopLimit": hop_limit}
        waits = want_ack or kind == "request"
        if kind == "text":
            packet = iface.sendData(
                str(value).encode("utf-8"),
                destinationId=dest,
                portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                wantAck=want_ack,
                onResponse=reply.on_response if want_ack else None,
                onResponseAckPermitted=want_ack,
                **common,
            )
        elif kind == "position":
            packet = iface.sendPosition(
                latitude=float(value["latitude"]),
                longitude=float(value["longitude"]),
                altitude=int(value.get("altitude", 0)),
                destinationId=dest,
                **common,
            )
        elif kind == "waypoint":
            packet = iface.sendWaypoint(
                name=str(value["name"]),
                description=str(value.get("description", "")),
                icon=value.get("icon", 0),
                expire=int(value.get("expire", 0)),
                latitude=float(value["latitude"]),
                longitude=float(value["longitude"]),
                destinationId=dest,
                wantAck=False,
                **common,
            )
        elif kind == "request":
            packet = _request(iface, dest, str(value), reply, channel, hop_limit)
        else:
            raise BackendError(f"cannot send {kind}={value!r}")
        out: dict[str, Any] = {
            "target": getattr(iface, "devPath", None) or target,
            "packet_id": getattr(packet, "id", None),
            "destination": dest if isinstance(dest, str) else f"!{dest:08x}",
            "kind": kind if kind != "request" else str(value).partition(":")[0],
        }
        if waits:
            reply.done.wait(wait_s)
            out["waited_s"] = round(time.monotonic() - started, 1)
            if kind == "request":
                decoded = (reply.packet or {}).get("decoded") or {}
                if reply.packet is None:
                    out["reply"] = None
                elif (
                    "routing" in decoded and decoded["routing"].get("errorReason", "NONE") != "NONE"
                ):
                    out["reply"] = {"error": decoded["routing"]["errorReason"]}
                else:
                    out["reply"] = _clean(_answer(decoded))
            else:
                out.update(_ack(reply, my_num, dest_num))
        # A broadcast still needs the port held while it waits its turn on the air.
        hold = LINGER_S if linger_s is None else linger_s
        time.sleep(max(0.0, hold - (time.monotonic() - started)))
    return out
