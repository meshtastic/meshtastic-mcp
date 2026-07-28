# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Post-suite mesh soak.

After the nightly suite finishes, the baked boards sit on their shared private
channel and mesh on their own. This service watches them for the configured
window, injects a sequenced text message on an interval (so mesh delivery is
measurable, not just incidental), and grabs periodic camera stills of the
device screens for the vision pass.

Capture is API-based, not raw-serial: firmware silences plain-text console
logging permanently (until reboot) on the first protobuf API touch of a port —
and the preflight, every send, and screen keep-alive are all such touches — so
a pyserial text reader records nothing (the runs-7–15 ``log_silence`` blackout).
Instead ``SoakObserver`` holds one persistent ``SerialInterface`` per fleet
device for the whole window and records received packets, telemetry,
``LogRecord`` lines, and connection drops (see ``soak_observer.py``). Sends and
keep-alive input events ride the same held interfaces, which also removes the
open/close churn that wedges nRF52 native-USB CDC.

Port discipline: for the soak window each fleet device is claimed in
``PortLocks`` (other port users fail fast with a clear error) and its raw
serial monitor is suspended; the observer tees records to the ``serial.*``
WS topics so a UI serial tab still shows live activity. Everything is released
and resumed on exit, even on error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..db import repo_cameras as rc
from ..db import repo_devices as rd
from . import camera_stream
from .nightly import NightlyConfig
from .soak_observer import SoakObserver

log = logging.getLogger("meshtastic_mcp.web.nightly_soak")

# The channel the bake provisions. Must match the ``channel_name`` argument the
# session profile passes to userprefs.build_testing_profile() in
# tests/conftest.py::test_profile — the soak preflight asserts the fleet is on
# this channel (and NOT on default LongFast) before trusting the night's mesh.
EXPECTED_CHANNEL = "McpTest"

# Names that mean "the bake did not stick" — firmware default channel.
DEFAULT_CHANNEL_NAMES = {"", "(default)", "LongFast"}

LOGS_FILE = "soak-logs.jsonl"
TELEMETRY_FILE = "soak-telemetry.jsonl"
SENDS_FILE = "soak-sends.jsonl"

# Owner label for PortLocks claims while the soak holds its API observers.
CLAIM_OWNER = "nightly soak"

_TICK_S = 5.0
# Floor on the periodic-action intervals — a pathological config must not turn
# the soak into a send/snapshot storm. Module-level so tests can shrink it.
MIN_ACTION_PERIOD_S = 60.0

# Battery percent from the firmware's periodic power log line (Power.cpp),
# e.g. "Battery: usbPower=0, isCharging=0, batMv=4011, batPct=87".
_BAT_RE = re.compile(r"batPct=(\d+)")

# async callable(severity, kind, message, data) -> None; the orchestrator binds
# step="soak" and persistence/WS fan-out behind it.
Observe = Callable[[str, str, str, dict | None], Awaitable[None]]


@dataclass
class SoakSummary:
    started_at: float
    ended_at: float = 0.0
    lines: dict[str, int] = field(default_factory=dict)  # serial -> captured records
    sends_attempted: int = 0
    sends_failed: int = 0
    snapshots: list[str] = field(default_factory=list)  # file names in the data dir
    preflight_failures: int = 0
    observers_opened: list[str] = field(default_factory=list)
    observers_failed: dict[str, str] = field(default_factory=dict)  # serial -> error
    observers_dropped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.ended_at - self.started_at, 1),
            "lines": dict(self.lines),
            "sends_attempted": self.sends_attempted,
            "sends_failed": self.sends_failed,
            "snapshots": len(self.snapshots),
            "preflight_failures": self.preflight_failures,
            "observers_opened": list(self.observers_opened),
            "observers_failed": dict(self.observers_failed),
            "observers_dropped": list(self.observers_dropped),
        }


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


class _JsonlWriter:
    """Append-only JSONL writer, safe to call from reader threads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


class NightlySoak:
    def __init__(
        self,
        db,
        serialmon,
        portlocks,
        *,
        cfg: NightlyConfig,
        nightly_id: int,
        data_dir: Path,
        observe: Observe,
        keepalive=None,
        hub=None,
    ) -> None:
        self.db = db
        self.serialmon = serialmon
        self.portlocks = portlocks
        self.cfg = cfg
        self.nightly_id = nightly_id
        self.data_dir = data_dir
        self.observe = observe
        self.keepalive = keepalive
        # WS hub for teeing observer records to the serial.* topics; falls back
        # to the serial monitor's hub so existing constructions keep working.
        self.hub = hub if hub is not None else getattr(serialmon, "hub", None)
        self.summary = SoakSummary(started_at=time.time())
        # Serials confirmed to be on the private bake channel — the ONLY devices
        # we may transmit to (a misbaked board would broadcast on public LongFast).
        self._verified: set[str] = set()

    # -- capture sink --------------------------------------------------------

    def _make_sink(self, logs: _JsonlWriter, telem: _JsonlWriter):
        counts = self.summary.lines

        def sink(rec: dict) -> None:
            serial = rec.get("serial") or rec.get("port") or "?"
            counts[serial] = counts.get(serial, 0) + 1
            logs.write(rec)
            heap = rec.get("heap_free")
            if heap is not None:
                telem.write(
                    {
                        "ts": rec["ts"],
                        "serial": serial,
                        "port": rec.get("port"),
                        "kind": "heap",
                        "value": heap,
                    }
                )
            m = _BAT_RE.search(rec.get("line") or "")
            if m:
                telem.write(
                    {
                        "ts": rec["ts"],
                        "serial": serial,
                        "port": rec.get("port"),
                        "kind": "battery",
                        "value": int(m.group(1)),
                    }
                )

        return sink

    # -- preflight -----------------------------------------------------------

    async def _preflight(self, observer: SoakObserver, fleet: list[dict]) -> None:
        """The userprefs guarantee: every fleet device must sit on the private
        bake channel, not LongFast defaults, before we trust the soak mesh.

        Reads channel/region off the observer's held interface (fetched during
        the connect handshake) — no extra connects. A device whose observer
        failed to open counts as a preflight failure: with no session there is
        no capture and no way to confirm its channel."""
        for row in fleet:
            serial = row["serial_number"]
            if not observer.is_held(serial):
                self.summary.preflight_failures += 1
                await self.observe(
                    "warn",
                    "soak.preflight_failed",
                    f"{serial}: could not read live config "
                    f"({self.summary.observers_failed.get(serial, 'no observer session')})",
                    {"serial": serial, "port": row.get("current_port")},
                )
                continue
            channel, region = observer.channel_and_region(serial)
            if channel != EXPECTED_CHANNEL or (channel or "") in DEFAULT_CHANNEL_NAMES:
                self.summary.preflight_failures += 1
                await self.observe(
                    "error",
                    "channel.default_profile",
                    f"{serial} is on channel {channel!r}, expected "
                    f"{EXPECTED_CHANNEL!r} — bake did not stick",
                    {"serial": serial, "channel": channel, "region": region},
                )
            elif not region or region == "UNSET":
                self.summary.preflight_failures += 1
                await self.observe(
                    "error",
                    "channel.region_unset",
                    f"{serial} has region {region!r} — TX is blocked",
                    {"serial": serial, "region": region},
                )
            else:
                # Positively confirmed on the private channel with a real region.
                self._verified.add(serial)

    # -- periodic actions ----------------------------------------------------

    async def _send_one(
        self, observer: SoakObserver, row: dict, seq: int, sends: _JsonlWriter
    ) -> None:
        serial, port = row["serial_number"], row.get("current_port")
        text = f"nightly-{self.nightly_id}-{seq}"
        self.summary.sends_attempted += 1
        ok = True
        error: str | None = None
        try:
            await observer.send_text(serial, text)
        except Exception as exc:
            ok = False
            error = str(exc)
            self.summary.sends_failed += 1
        sends.write(
            {
                "ts": time.time(),
                "seq": seq,
                "serial": serial,
                "port": port,
                "text": text,
                "ok": ok,
                "error": error,
            }
        )

    async def _snapshot_all(self) -> None:
        cameras = [
            c
            for c in await rc.list_all(self.db)
            if c.get("enabled") and c.get("device_serial") and c.get("device_index")
        ]
        for cam in cameras:
            jpg = await asyncio.to_thread(
                camera_stream.snapshot,
                cam["device_index"],
                rotation=int(cam.get("rotation") or 0),
                mirror=bool(cam.get("mirror")),
            )
            if jpg is None:
                continue
            name = f"snap-{cam['device_serial']}-{int(time.time())}.jpg"
            try:
                (self.data_dir / name).write_bytes(jpg)
                self.summary.snapshots.append(name)
            except OSError as exc:
                log.debug("could not save snapshot %s: %s", name, exc)

    # -- main loop -----------------------------------------------------------

    async def run(self, duration_s: float, cancel: asyncio.Event | None = None) -> SoakSummary:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logs = _JsonlWriter(self.data_dir / LOGS_FILE)
        telem = _JsonlWriter(self.data_dir / TELEMETRY_FILE)
        sends = _JsonlWriter(self.data_dir / SENDS_FILE)
        sink = self._make_sink(logs, telem)

        fleet = await rd.online_with_env(self.db)
        if not fleet:
            await self.observe("warn", "soak.no_fleet", "no online fleet devices to soak", None)
        node_map = {row["node_num"]: row["serial_number"] for row in fleet if row.get("node_num")}
        observer = SoakObserver(
            on_record=sink, on_telemetry=telem.write, hub=self.hub, node_map=node_map
        )

        if self.cfg.soak_keepalive and self.keepalive is not None:
            # ScreenKeepAlive.cfg is a plain dict (see services/keepalive.py).
            ka_cfg = getattr(self.keepalive, "cfg", None)
            enabled = bool(ka_cfg.get("enabled")) if isinstance(ka_cfg, dict) else False
            if not enabled:
                await self.observe(
                    "info",
                    "soak.keepalive_off",
                    "screen keep-alive is disabled — device screens may sleep "
                    "and camera snapshots may show blank displays",
                    None,
                )

        cameras_present = any(
            c.get("enabled") and c.get("device_serial") for c in await rc.list_all(self.db)
        )
        if not cameras_present:
            await self.observe(
                "info", "soak.no_cameras", "no assigned cameras — soak runs without snapshots", None
            )

        # Take ownership of every fleet device for the window: claim it (other
        # port users fail fast), suspend its raw serial monitor (a text reader
        # would fight the interface for bytes — and captures nothing anyway,
        # see the module docstring), then hold a persistent API observer on it.
        claimed: list[str] = []
        suspended: list[str] = []
        try:
            for row in fleet:
                serial, port = row["serial_number"], row.get("current_port")
                if not port:
                    continue
                try:
                    self.portlocks.claim(serial, CLAIM_OWNER)
                except Exception as exc:
                    self.summary.observers_failed[serial] = str(exc)
                    continue
                claimed.append(serial)
                # The claim stops new port users, but one may be mid-flight
                # (enrichment/keep-alive inside guard()) — wait it out so the
                # observer's non-blocking open doesn't lose the race.
                await self.portlocks.wait_clear(serial)
                try:
                    await self.serialmon.suspend(serial)
                    suspended.append(serial)
                except Exception:
                    log.debug("soak could not suspend monitor for %s", serial, exc_info=True)
                if not await observer.open_device(serial, port):
                    # The observer holds nothing — release the claim (and give
                    # the monitor back) so keep-alive/control fall back to their
                    # normal port paths instead of failing against a ghost claim.
                    self.portlocks.release_claim(serial, CLAIM_OWNER)
                    claimed.remove(serial)
                    if serial in suspended:
                        suspended.remove(serial)
                        try:
                            await self.serialmon.resume(serial)
                        except Exception:
                            log.debug("soak could not resume monitor for %s", serial, exc_info=True)
            self.summary.observers_opened = list(observer.summary.opened)
            self.summary.observers_failed.update(observer.summary.open_failed)

            await self._preflight(observer, fleet)

            # Route keep-alive input events through the held interfaces so the
            # screens stay awake for the cameras without touching the ports.
            if self.keepalive is not None:
                self.keepalive.relay = observer

            deadline = time.monotonic() + duration_s
            traffic_period = max(MIN_ACTION_PERIOD_S, self.cfg.soak_traffic_interval_min * 60.0)
            snap_period = max(MIN_ACTION_PERIOD_S, self.cfg.soak_snapshot_interval_min * 60.0)
            next_send = time.monotonic() + traffic_period
            next_snap = time.monotonic() + snap_period
            # Continue the send sequence across a mid-soak restart so a resumed
            # night never reuses an id already on the wire (the file is
            # append-only).
            seq = _count_lines(self.data_dir / SENDS_FILE)
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    break
                await observer.health_tick()
                now = time.monotonic()
                if now >= next_send:
                    next_send = now + traffic_period
                    # Transmit ONLY from boards confirmed on the private channel —
                    # never from a misbaked board that would broadcast on LongFast.
                    senders = [d for d in fleet if d["serial_number"] in self._verified]
                    if senders:
                        await self._send_one(observer, senders[seq % len(senders)], seq, sends)
                        seq += 1
                if cameras_present and now >= next_snap:
                    next_snap = now + snap_period
                    await self._snapshot_all()
                await asyncio.sleep(min(_TICK_S, max(0.1, deadline - time.monotonic())))
        finally:
            if self.keepalive is not None and getattr(self.keepalive, "relay", None) is observer:
                self.keepalive.relay = None
            await observer.close_all()
            self.summary.observers_dropped = list(observer.summary.dropped)
            for serial in claimed:
                self.portlocks.release_claim(serial, CLAIM_OWNER)
            for serial in suspended:
                try:
                    await self.serialmon.resume(serial)
                except Exception:
                    log.debug("soak could not resume monitor for %s", serial, exc_info=True)
            logs.close()
            telem.close()
            sends.close()
            self.summary.ended_at = time.time()
        return self.summary
