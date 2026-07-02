# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet screen keep-alive.

Keeps every connected node's OLED awake so the cameras always have something to
watch, in two ways the operator asked for:

1. Provision: once per device, set ``display.screen_on_secs`` high so the screen
   doesn't time out on its own (survives pauses, e.g. a test run).
2. Periodic input: every ``interval_s`` inject an input-broker event (an admin
   message — default the user-button short press, which also advances the screen
   carousel) so the display stays awake and cycles through frames on camera.

Gated like every other port-bound action: skipped while a test run owns the
ports, serialised so one device is on the wire at a time, and the device's live
serial monitor is suspended for the connect and resumed after.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..db import repo_devices as rd
from ..db import repo_settings as rs

log = logging.getLogger("meshtastic_mcp.web.keepalive")

# screen_on_secs is multiplied by 1000 (ms) in firmware — keep it well under the
# uint32 ceiling. One day is effectively "always on" for a bench and is refreshed
# by the periodic input events anyway.
_MAX_SCREEN_ON = 86_400

DEFAULTS = {
    "enabled": False,
    "interval_s": 30,
    "event": "USER_PRESS",
    "screen_on_secs": _MAX_SCREEN_ON,
}


class ScreenKeepAlive:
    def __init__(self, db, hub, serialmon=None, portlocks=None) -> None:
        self.db = db
        self.hub = hub
        self.serialmon = serialmon
        from .portlock import PortLocks

        self.portlocks = portlocks or PortLocks(serialmon)
        self.cfg = dict(DEFAULTS)
        self.stats = {
            "enabled": False,
            "provisioned": 0,
            "events_sent": 0,
            "last_error": None,
            "last_cycle_ts": None,
        }
        self._task: asyncio.Task | None = None
        self._provisioned: set[str] = set()  # serials whose screen we've pinned

    def status(self) -> dict:
        return {"config": dict(self.cfg), "stats": dict(self.stats)}

    async def reload(self) -> None:
        stored = await rs.get_json(self.db, "keepalive") or {}
        self.cfg = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}

    async def save(self, patch: dict) -> None:
        for k in DEFAULTS:
            if k in patch and patch[k] is not None:
                self.cfg[k] = patch[k]
        await rs.set_json(self.db, "keepalive", self.cfg)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["last_error"] = str(exc)
                log.debug("keepalive cycle error: %s", exc)
            await asyncio.sleep(max(5, int(self.cfg.get("interval_s") or 30)))

    async def _cycle(self) -> None:
        from . import test_runner

        self.stats["enabled"] = bool(self.cfg.get("enabled"))
        if not self.cfg.get("enabled") or test_runner.is_running():
            return
        rows = [d for d in await rd.list_all(self.db) if d.get("online") and d.get("kind") == "usb"]
        for d in rows:
            await self._touch(d)
        self.stats["last_cycle_ts"] = time.time()
        await self.hub.publish("keepalive.update", self.status())

    async def _touch(self, device: dict) -> None:
        from meshtastic_mcp import admin

        from . import test_runner

        serial = device["serial_number"]
        port = device.get("current_port")
        if not port:
            return
        async with self.portlocks.guard(serial):  # exclusive device access
            if test_runner.is_running():
                return
            try:
                if serial not in self._provisioned:
                    await asyncio.to_thread(
                        admin.set_config,
                        "display.screen_on_secs",
                        int(self.cfg["screen_on_secs"]),
                        port,
                    )
                    self._provisioned.add(serial)
                    self.stats["provisioned"] = len(self._provisioned)
                await asyncio.to_thread(admin.send_input_event, self.cfg["event"], 0, 0, 0, port)
                self.stats["events_sent"] += 1
                self.stats["last_error"] = None
            except Exception as exc:
                self.stats["last_error"] = f"{serial[:8]}: {exc}"
                log.debug("keepalive %s failed: %s", serial, exc)
