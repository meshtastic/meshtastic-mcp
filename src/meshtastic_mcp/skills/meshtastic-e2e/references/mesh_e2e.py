#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Device-plane helper for Meshtastic device<->app E2E loops.

Run with the MCP server's venv so meshtastic + meshtastic_mcp are importable:

    python references/mesh_e2e.py <cmd> [args]

Commands
  devices                                  list USB-serial radios (port, vid, desc)
  info        <port>                        device_info summary
  send        <port> <text> [--dest ID]    send a text (broadcast or directed)
  recv-text   <port> <token> [--secs 30]   open iface, wait for a TEXT_MESSAGE_APP
                                            containing <token>, print PASS/FAIL+latency
  watch-tx    <token> [--secs 30]          poll the recorder packets_window for a
                                            TEXT_MESSAGE_APP carrying <token> (app->device
                                            oracle; recorder must be capturing)
  traceroute  <port> <dest> [--secs 45]    non-blocking traceroute, print route+SNR
  recorder    <port> [--secs 60]           hold a connection open and capture to .mtlog

All commands are bounded (no infinite blocks) and print a single-line verdict where
relevant so a calling agent can grep the result.

These wrap exactly the APIs verified to work against a Seeed Tracker L1 on 2.7.25:
meshtastic.serial_interface.SerialInterface, iface.sendText/sendData, and
meshtastic_mcp.log_query.packets_window over the recorder JSONL.
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def _iface(port: str):
    import meshtastic.serial_interface as si

    return si.SerialInterface(port)


def cmd_devices(_args) -> int:
    from meshtastic_mcp import devices

    for d in devices.list_devices():
        print(f"{d['port']}  {d.get('vid')}  {d.get('description')}")
    return 0


def cmd_info(args) -> int:
    from meshtastic_mcp import info

    print(json.dumps(info.device_info(args.port), indent=2, default=str))
    return 0


def cmd_send(args) -> int:
    i = _iface(args.port)
    try:
        i.sendText(args.text, destinationId=args.dest or "^all", wantAck=bool(args.dest))
        print(f"SENT token={args.text!r} dest={args.dest or '^all'}")
    finally:
        i.close()
    return 0


def cmd_recv_text(args) -> int:
    """Device-plane oracle: this radio must RECEIVE a text carrying the token."""
    from pubsub import pub

    hits: list[dict] = []

    def on_text(packet=None, interface=None, **_):
        d = (packet or {}).get("decoded") or {}
        if d.get("portnum") == "TEXT_MESSAGE_APP":
            txt = d.get("payload") or b""
            txt = (
                txt.decode("utf-8", "replace") if isinstance(txt, (bytes, bytearray)) else str(txt)
            )
            if args.token in txt:
                hits.append(
                    {
                        "from": packet.get("fromId"),
                        "text": txt,
                        "snr": packet.get("rxSnr"),
                        "rssi": packet.get("rxRssi"),
                    }
                )

    pub.subscribe(on_text, "meshtastic.receive.text")
    i = _iface(args.port)
    t0 = time.time()
    try:
        while time.time() - t0 < args.secs and not hits:
            time.sleep(0.5)
    finally:
        i.close()
    if hits:
        print(f"PASS token={args.token!r} latency={int((time.time() - t0) * 1000)}ms {hits[0]}")
        return 0
    print(f"FAIL token={args.token!r} no TEXT_MESSAGE_APP within {args.secs}s")
    return 1


def cmd_watch_tx(args) -> int:
    """App->device oracle: poll recorder packets.jsonl for the token."""
    from meshtastic_mcp import log_query as q

    t0 = time.time()
    while time.time() - t0 < args.secs:
        win = q.packets_window(max=40)
        rows = win.get("packets", win) if isinstance(win, dict) else win
        for p in rows if isinstance(rows, list) else []:
            if p.get("portnum") == "TEXT_MESSAGE_APP" and args.token in json.dumps(p, default=str):
                print(
                    f"PASS token={args.token!r} from={p.get('from_node')} "
                    f"latency={int((time.time() - t0) * 1000)}ms"
                )
                return 0
        time.sleep(1)
    print(f"FAIL token={args.token!r} not seen in recorder within {args.secs}s")
    return 1


def cmd_traceroute(args) -> int:
    from meshtastic import mesh_pb2, portnums_pb2

    replies: list[dict] = []
    i = _iface(args.port)
    try:
        i.sendData(
            mesh_pb2.RouteDiscovery(),
            destinationId=args.dest,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantResponse=True,
            onResponse=lambda p: replies.append(p),
            hopLimit=7,
        )
        t0 = time.time()
        while time.time() - t0 < args.secs and not replies:
            time.sleep(0.5)
    finally:
        i.close()
    if not replies:
        print(f"FAIL traceroute {args.dest} no reply within {args.secs}s (stale/unreachable)")
        return 1
    for p in replies:
        rd = (p.get("decoded") or {}).get("traceroute") or {}
        toward = [hex(x) for x in rd.get("route", [])]
        back = [hex(x) for x in rd.get("routeBack", [])]
        snr_t = [s / 4 for s in rd.get("snrTowards", [])]
        snr_b = [s / 4 for s in rd.get("snrBack", [])]
        print(
            f"PASS traceroute {args.dest} hops_toward={len(toward)} "
            f"route={toward or 'direct'} snrTowards={snr_t} "
            f"routeBack={back or 'direct'} snrBack={snr_b}"
        )
    return 0


def cmd_recorder(args) -> int:
    from meshtastic_mcp.recorder import get_recorder

    rec = get_recorder()
    rec.start()
    rec.mark_event(label="e2e_soak_start", note=f"{args.secs}s")
    i = _iface(args.port)
    try:
        time.sleep(args.secs)
    finally:
        rec.mark_event(label="e2e_soak_end")
        i.close()
    print(json.dumps(rec.status(), indent=2, default=str))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    sp = sub.add_parser("info")
    sp.add_argument("port")
    sp = sub.add_parser("send")
    sp.add_argument("port")
    sp.add_argument("text")
    sp.add_argument("--dest")
    sp = sub.add_parser("recv-text")
    sp.add_argument("port")
    sp.add_argument("token")
    sp.add_argument("--secs", type=int, default=30)
    sp = sub.add_parser("watch-tx")
    sp.add_argument("token")
    sp.add_argument("--secs", type=int, default=30)
    sp = sub.add_parser("traceroute")
    sp.add_argument("port")
    sp.add_argument("dest")
    sp.add_argument("--secs", type=int, default=45)
    sp = sub.add_parser("recorder")
    sp.add_argument("port")
    sp.add_argument("--secs", type=int, default=60)
    args = p.parse_args(argv)
    return {
        "devices": cmd_devices,
        "info": cmd_info,
        "send": cmd_send,
        "recv-text": cmd_recv_text,
        "watch-tx": cmd_watch_tx,
        "traceroute": cmd_traceroute,
        "recorder": cmd_recorder,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
