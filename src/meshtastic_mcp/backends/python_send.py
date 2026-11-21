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


def send(
    target: str | None,
    to: str | int | None,
    payload: dict[str, Any],
    channel: int,
    want_ack: bool,
    hop_limit: int | None,
    wait_s: float,
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
        elif kind == "request" and value == "traceroute":
            route = mesh_pb2.RouteDiscovery()
            packet = iface.sendData(
                route,
                destinationId=dest,
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
                onResponse=reply.on_response,
                channelIndex=channel,
                hopLimit=hop_limit or 7,
            )
        elif kind == "request" and value == "telemetry":
            request = telemetry_pb2.Telemetry()
            request.device_metrics.SetInParent()
            packet = iface.sendData(
                request,
                destinationId=dest,
                portNum=portnums_pb2.PortNum.TELEMETRY_APP,
                wantResponse=True,
                onResponse=reply.on_response,
                **common,
            )
        else:
            raise BackendError(f"cannot send {kind}={value!r}")
        out: dict[str, Any] = {
            "target": getattr(iface, "devPath", None) or target,
            "packet_id": getattr(packet, "id", None),
            "destination": dest if isinstance(dest, str) else f"!{dest:08x}",
            "kind": kind if kind != "request" else str(value),
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
                    out["reply"] = _clean(decoded.get("traceroute") or decoded.get("telemetry"))
            else:
                out.update(_ack(reply, my_num, dest_num))
        # A broadcast still needs the port held while it waits its turn on the air.
        time.sleep(max(0.0, LINGER_S - (time.monotonic() - started)))
    return out
