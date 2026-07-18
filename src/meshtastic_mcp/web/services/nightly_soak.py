# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Post-suite mesh soak.

After the nightly suite finishes, the baked boards sit on their shared private
channel and mesh on their own. This service watches them for the configured
window: it tees every serial-monitor line into per-night JSONL files (the web
monitors do NOT feed the MCP recorder, so without this the soak would be
invisible to analysis), injects a sequenced text message on an interval (so
mesh delivery is measurable, not just incidental), and grabs periodic camera
stills of the device screens for the vision pass.

Everything here rides the existing port arbitration: line capture uses the
already-open serial monitors via a sink, and the occasional port-bound action
(preflight ``device_info``, ``send_text``) goes through ``portlocks.guard``
exactly like keep-alive does. The soak itself never opens a port.
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

from meshtastic_mcp import admin
from meshtastic_mcp import info as mt_info

from ..db import repo_cameras as rc
from ..db import repo_devices as rd
from . import camera_stream
from .nightly import NightlyConfig

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
    lines: dict[str, int] = field(default_factory=dict)  # serial -> captured lines
    sends_attempted: int = 0
    sends_failed: int = 0
    snapshots: list[str] = field(default_factory=list)  # file names in the data dir
    preflight_failures: int = 0

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
        }


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
    ) -> None:
        self.db = db
        self.serialmon = serialmon
        self.portlocks = portlocks
        self.cfg = cfg
        self.nightly_id = nightly_id
        self.data_dir = data_dir
        self.observe = observe
        self.keepalive = keepalive
        self.summary = SoakSummary(started_at=time.time())

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

    async def _preflight(self) -> None:
        """The userprefs guarantee: every fleet device must sit on the private
        bake channel, not LongFast defaults, before we trust the soak mesh."""
        fleet = await rd.online_with_env(self.db)
        if not fleet:
            await self.observe("warn", "soak.no_fleet", "no online fleet devices to soak", None)
            return
        for row in fleet:
            serial, port = row["serial_number"], row.get("current_port")
            if not port:
                continue
            try:
                async with self.portlocks.guard(serial):
                    live = await asyncio.to_thread(mt_info.device_info, port)
            except Exception as exc:
                self.summary.preflight_failures += 1
                await self.observe(
                    "warn",
                    "soak.preflight_failed",
                    f"{serial}: could not read live config ({exc})",
                    {"serial": serial, "port": port},
                )
                continue
            channel = live.get("primary_channel")
            region = live.get("region")
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

    # -- periodic actions ----------------------------------------------------

    async def _send_one(self, row: dict, seq: int, sends: _JsonlWriter) -> None:
        serial, port = row["serial_number"], row.get("current_port")
        text = f"nightly-{self.nightly_id}-{seq}"
        self.summary.sends_attempted += 1
        ok = True
        error: str | None = None
        try:
            async with self.portlocks.guard(serial):
                await asyncio.to_thread(admin.send_text, text, None, 0, False, port)
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

        await self._preflight()

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

        # Hold a logging monitor open on every online fleet device for the whole
        # soak, so the sink actually sees lines. Without this, capture depends on
        # something ELSE keeping the reader open (a UI serial tab, or FleetLog
        # when a Datadog key is set) — which is not guaranteed at 3am.
        held: list[str] = []
        for row in await rd.online_with_env(self.db):
            serial = row["serial_number"]
            try:
                await self.serialmon.acquire(serial)
                held.append(serial)
            except Exception as exc:
                log.debug("soak could not acquire monitor for %s: %s", serial, exc)

        self.serialmon.sinks.append(sink)
        deadline = time.monotonic() + duration_s
        traffic_period = max(MIN_ACTION_PERIOD_S, self.cfg.soak_traffic_interval_min * 60.0)
        snap_period = max(MIN_ACTION_PERIOD_S, self.cfg.soak_snapshot_interval_min * 60.0)
        next_send = time.monotonic() + traffic_period
        next_snap = time.monotonic() + snap_period
        seq = 0
        try:
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    break
                now = time.monotonic()
                if now >= next_send:
                    next_send = now + traffic_period
                    fleet = await rd.online_with_env(self.db)
                    if fleet:
                        await self._send_one(fleet[seq % len(fleet)], seq, sends)
                        seq += 1
                if cameras_present and now >= next_snap:
                    next_snap = now + snap_period
                    await self._snapshot_all()
                await asyncio.sleep(min(_TICK_S, max(0.1, deadline - time.monotonic())))
        finally:
            try:
                self.serialmon.sinks.remove(sink)
            except ValueError:
                pass
            for serial in held:
                try:
                    await self.serialmon.release(serial)
                except Exception:
                    log.debug("soak could not release monitor for %s", serial, exc_info=True)
            logs.close()
            telem.close()
            sends.close()
            self.summary.ended_at = time.time()
        return self.summary
