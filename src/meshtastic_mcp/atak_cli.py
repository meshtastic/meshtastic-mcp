# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""``meshtastic-mcp-atak`` — drive the ATAK emulator fleet + CoT relay from bash.

Wraps :mod:`meshtastic_mcp.emulator.atak` and
:class:`meshtastic_mcp.replay.cot_relay.CotRelay` directly — not the MCP
server — so a fix to the library is exercised by the next shell command, with
no server reconnect. Stdlib only; every failure exits non-zero with the
:class:`~meshtastic_mcp.emulator.atak.AtakError` message on stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any, cast

from . import config
from .emulator import atak
from .replay.cot_relay import CotRelay

_STATUS_EVERY_S = 30.0


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _latlon(text: str) -> tuple[float, float]:
    try:
        lat_s, lon_s = text.split(",", 1)
        return float(lat_s), float(lon_s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected LAT,LON, got {text!r}") from None


def _positive(text: str) -> float:
    val = float(text)
    if val <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text!r}")
    return val


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_relay_start(args: argparse.Namespace) -> int:
    name = args.session or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    outdir = config.mcp_data_dir() / "cot_captures" / name
    relay = CotRelay(outdir=outdir, port=args.port)
    relay.start()
    print(f"relay listening on {relay.host}:{relay.port}")
    print(f"capturing to {outdir}")
    try:
        while True:
            time.sleep(_STATUS_EVERY_S)
            st = relay.status()
            peers = ", ".join(
                f"{p['callsign'] or '?'}@{p['addr']}({p['events']})"
                for p in cast(list[dict[str, Any]], st["peers"])
            )
            _log(f"peers={st['peer_count']} [{peers}] types={json.dumps(st['type_counts'])}")
    except KeyboardInterrupt:
        pass
    finally:
        relay.stop()
    print(json.dumps(relay.status(), indent=2))
    return 0


def cmd_fleet_up(args: argparse.Namespace) -> int:
    _log(f"fleet up: count={args.count} base={args.base_avd} relay_port={args.relay_port}")
    fleet = atak.fleet_up(
        args.count,
        args.apk,
        base_avd=args.base_avd,
        relay_port=args.relay_port,
        use_snapshot=not args.no_snapshot,
    )
    _log(f"fleet ready: {fleet.serials()}")
    print(json.dumps([n.__dict__ for n in fleet.nodes], indent=2))
    return 0


def cmd_fleet_down(args: argparse.Namespace) -> int:
    fleet = atak.discover_fleet()
    _log(f"fleet down: {fleet.serials() or 'no running nodes'}")
    atak.fleet_down(fleet, delete_clones=args.delete_clones)
    if args.delete_clones:
        # Clones that were not running are outside the discovered fleet.
        for name in atak.list_clone_avds():
            _log(f"deleting clone {name}")
            atak.delete_clone_avd(name)
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    _log(f"drive {args.serial}: {len(args.waypoints)} waypoints @ {args.speed} m/s")
    try:
        atak.drive_route(args.serial, args.waypoints, speed_mps=args.speed, step_s=args.step)
    except KeyboardInterrupt:
        _log("drive interrupted")
        return 130
    _log("route complete")
    return 0


def cmd_position(args: argparse.Namespace) -> int:
    kwargs = {"speed_mps": args.speed} if args.speed is not None else {}
    atak.set_position(args.serial, args.lat, args.lon, **kwargs)
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    _log(f"provision {args.serial} apk={args.apk} relay_port={args.relay_port}")
    atak.provision(args.serial, args.apk, relay_port=args.relay_port)
    _log("provisioned")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meshtastic-mcp-atak",
        description="ATAK emulator fleet + CoT relay, straight from the library.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    relay = sub.add_parser("relay", help="CoT capture + relay server").add_subparsers(
        dest="relay_cmd", required=True
    )
    rs = relay.add_parser("start", help="run a relay in the foreground (Ctrl-C stops)")
    rs.add_argument("--port", type=int, default=8087)
    rs.add_argument("--session", help="capture dir name (default: UTC timestamp)")
    rs.set_defaults(func=cmd_relay_start)

    fleet = sub.add_parser("fleet", help="emulator fleet").add_subparsers(
        dest="fleet_cmd", required=True
    )
    fu = fleet.add_parser("up", help="clone, boot and provision N nodes")
    fu.add_argument("--count", type=int, required=True)
    fu.add_argument("--apk", required=True, help="ATAK-CIV APK path")
    fu.add_argument("--base-avd", required=True, help="AVD to clone from")
    fu.add_argument("--relay-port", type=int, default=8087)
    fu.add_argument("--no-snapshot", action="store_true", help="ignore provisioned snapshots")
    fu.set_defaults(func=cmd_fleet_up)
    fd = fleet.add_parser("down", help="stop every running atak-node-* emulator")
    fd.add_argument("--delete-clones", action="store_true", help="also delete the cloned AVDs")
    fd.set_defaults(func=cmd_fleet_down)

    dr = sub.add_parser("drive", help="feed a moving GPS track (foreground)")
    dr.add_argument("serial")
    dr.add_argument("--speed", type=_positive, default=10.0, help="m/s")
    dr.add_argument("--step", type=_positive, default=2.0, help="seconds between fixes")
    dr.add_argument("waypoints", type=_latlon, nargs="+", metavar="LAT,LON")
    # A southern/western waypoint ("-33.9,151.2") starts with "-"; argparse's
    # stock negative-number matcher only recognises bare numbers, so widen it
    # to "-<num>," for this subparser or the token is read as an option.
    dr._negative_number_matcher = re.compile(r"^-\d+(?:\.\d*)?,")
    dr.set_defaults(func=cmd_drive)

    po = sub.add_parser("position", help="set a single GPS fix")
    po.add_argument("serial")
    po.add_argument("lat", type=float)
    po.add_argument("lon", type=float)
    po.add_argument("--speed", type=float, default=None, help="m/s (optional)")
    po.set_defaults(func=cmd_position)

    pr = sub.add_parser("provision", help="install + provision ATAK on a booted emulator")
    pr.add_argument("serial")
    pr.add_argument("--apk", required=True)
    pr.add_argument("--relay-port", type=int, default=8087)
    pr.set_defaults(func=cmd_provision)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except atak.AtakError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
