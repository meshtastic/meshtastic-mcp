"""Background USB discovery loop.

Polls the serial bus, reconciles each likely-Meshtastic device into the
registry (keyed by stable serial, surrogate key otherwise), flips vanished
devices offline, and broadcasts the deltas on ``device.update``.

Auto-enrichment: when a device is newly seen (or hops ports, or hasn't been
read yet), the loop fires a one-shot ``device_info`` in the background to sniff
its firmware version, hw_model → exact pio env, region, and node num — so those
populate on their own at plug-in, FleetLog-style. It is gated hard for safety:
skipped entirely while a test run holds the ports, serialized so only one
device is connected at a time, the device's live serial monitor is suspended
for the moment of the connect, pinned envs are never clobbered, and failures
back off instead of re-hammering every poll. Disable with
``MESHTASTIC_MCP_AUTO_ENRICH=0``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from meshtastic_mcp import devices as devices_lib

from ..db import repo_devices as rd
from . import identity

log = logging.getLogger("meshtastic_mcp.web.discovery")

POLL_SECONDS = 4.0
ENRICH_BACKOFF_S = 60.0  # after a failed connect, wait this long before retrying
UNWEDGE_COOLDOWN_S = 300.0  # don't auto-power-cycle the same device more than once / 5 min


def _port_locations() -> dict[str, str]:
    """Map each serial device path → its USB topology location (e.g.
    ``/dev/cu.usbmodem143101`` → ``20-3.1``), via pyserial. Blocking."""
    try:
        from serial.tools import list_ports

        return {p.device: p.location for p in list_ports.comports() if p.location}
    except Exception:
        return {}


class DeviceDiscovery:
    def __init__(self, db, hub, serialmon=None, forwarder=None, portlocks=None) -> None:
        self.db = db
        self.hub = hub
        self.serialmon = serialmon
        self.forwarder = forwarder
        from .portlock import PortLocks

        self.portlocks = portlocks or PortLocks(serialmon)
        self.auto_enrich = os.environ.get("MESHTASTIC_MCP_AUTO_ENRICH", "1") != "0"
        # Self-heal a genuinely wedged device by power-cycling its hub slot.
        # Default on; disable with MESHTASTIC_MCP_AUTO_UNWEDGE=0.
        self.auto_unwedge = os.environ.get("MESHTASTIC_MCP_AUTO_UNWEDGE", "1") != "0"
        self._task: asyncio.Task | None = None
        self._enriched: dict[str, str] = {}  # serial -> port last enriched at
        self._failed: dict[str, float] = {}  # serial -> monotonic time to retry after
        self._enriching: set[str] = set()  # in-flight, to dedupe schedules
        self._unwedge_at: dict[str, float] = {}  # serial -> monotonic cooldown expiry

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def scan_once(self) -> None:
        """One discovery pass. Runs the (blocking) enumeration in a thread."""
        try:
            # include_unknown=True so we still see boards behind generic UART
            # bridges (CP210x/CH34x): meshtastic's findPorts only returns
            # allowlisted VIDs when any are present, hiding e.g. a Heltec V3's
            # CP2102. We re-filter below on FleetSuite's own criteria.
            found = await asyncio.to_thread(devices_lib.list_devices, True)
            # pyserial carries the USB topology (location) that list_devices drops;
            # we use it to map each device to its uhubctl hub port deterministically.
            locations = await asyncio.to_thread(_port_locations)
        except Exception as exc:
            log.debug("discovery enumeration failed: %s", exc)
            return

        seen: set[str] = set()
        for dev in found:
            vid = dev.get("vid")
            if dev.get("blacklisted"):
                continue
            # Keep meshtastic-likely ports plus anything on a board VID we
            # recognise (covers CP210x/CH34x ESP32 boards findPorts drops).
            if not (dev.get("likely_meshtastic") or identity.role_for_vid(vid)):
                continue
            loc = locations.get(dev.get("port"))
            key, _stable = identity.device_key({**dev, "location": loc})
            role = identity.role_for_vid(vid)
            seen.add(key)
            row = await rd.upsert_from_discovery(
                self.db,
                serial_number=key,
                current_port=dev.get("port"),
                vid=dev.get("vid"),
                pid=dev.get("pid"),
                role=role,
            )
            changed = (
                bool(row.pop("_is_new", False))
                | bool(row.pop("_port_changed", False))
                | bool(row.pop("_came_online", False))
            )
            # Auto-map the uhubctl hub port from USB topology (no power-cycling).
            slot = identity.hub_slot_from_location(loc)
            if slot and (row.get("hub_location") != slot[0] or row.get("hub_port") != slot[1]):
                row = await rd.set_hub_port(self.db, key, location=slot[0], port=slot[1])
                changed = True
            if changed:
                await self.hub.publish("device.update", row)
            self._maybe_enrich(row, changed)

        # Keep native nodes alive across scans — they aren't on the USB bus.
        natives = {
            d["serial_number"] for d in await rd.list_all(self.db) if d.get("kind") == "native"
        }
        newly_offline = await rd.mark_offline_except(self.db, seen | natives)
        for serial in newly_offline:
            self._enriched.pop(serial, None)  # re-verify if it comes back
            row = await rd.get(self.db, serial)
            if row:
                await self.hub.publish("device.update", row)

        # Keep the Datadog fleet-log capture in sync with what's online — but
        # NOT while a test run owns the ports (sync_capture acquires serial
        # monitors, which would re-open ports the pytest subprocess needs).
        from . import test_runner

        if self.forwarder is not None and not test_runner.is_running():
            await self.forwarder.sync_capture(seen)

    # --- auto-enrichment --------------------------------------------------
    def _maybe_enrich(self, row: dict, changed: bool) -> None:
        """Decide whether a discovered device needs a background enrichment and,
        if so, schedule one. Cheap, synchronous gate; the actual connect happens
        in :meth:`_enrich`."""
        if not self.auto_enrich:
            return
        serial = row.get("serial_number")
        if not serial or row.get("kind") == "native":
            return
        # Lazy import avoids a module-load cycle (test_runner ← control ← ...).
        from . import test_runner

        if test_runner.is_running():
            return
        if serial in self._enriching:
            return
        retry_at = self._failed.get(serial)
        if retry_at is not None and time.monotonic() < retry_at:
            return
        # Enrich once per (serial, port). A completed-but-incomplete read sets a
        # backoff (handled in _enrich), so we never reconnect every poll.
        needs = changed or self._enriched.get(serial) != row.get("current_port")
        if needs:
            asyncio.create_task(self._enrich(serial))

    async def _enrich(self, serial: str) -> None:
        from meshtastic_mcp import info as mt_info

        from . import test_runner

        if serial in self._enriching:
            return
        self._enriching.add(serial)
        try:
            row = await rd.get(self.db, serial)
            if (
                test_runner.is_running()
                or row is None
                or not row.get("online")
                or (row.get("kind") == "native")
            ):
                return
            port = row.get("current_port")
            if not port:
                return
            # Exclusive device access for the connect (suspends the monitor).
            async with self.portlocks.guard(serial):
                if test_runner.is_running():
                    return
                info = await asyncio.to_thread(mt_info.device_info, port)

                hw_model = info.get("hw_model")
                env = identity.env_for_hw_model(hw_model) if hw_model else None
                updated = await rd.update_enrichment(
                    self.db,
                    serial,
                    node_num=info.get("my_node_num"),
                    env=env,
                    hw_model=str(hw_model) if hw_model else None,
                    firmware_version=info.get("firmware_version"),
                    region=info.get("region"),
                )
                if info.get("firmware_version"):
                    # Full read — terminal for this port.
                    self._enriched[serial] = port
                    self._failed.pop(serial, None)
                    log.info(
                        "enriched %s: fw=%s hw=%s env=%s",
                        serial,
                        info.get("firmware_version"),
                        hw_model,
                        env,
                    )
                else:
                    # Connected but metadata wasn't ready yet — retry after backoff.
                    self._failed[serial] = time.monotonic() + ENRICH_BACKOFF_S
                if updated:
                    await self.hub.publish("device.update", updated)
        except Exception as exc:
            self._failed[serial] = time.monotonic() + ENRICH_BACKOFF_S
            log.debug("enrichment of %s failed (backing off): %s", serial, exc)
            # If the failure is a genuinely wedged port, self-heal it.
            row = await rd.get(self.db, serial)
            if row:
                await self._maybe_unwedge(serial, row)
        finally:
            self._enriching.discard(serial)

    async def _maybe_unwedge(self, serial: str, row: dict) -> None:
        """Self-heal a wedged device by power-cycling its hub slot, once.

        Only fires for a *genuine* wedge: a non-exclusive open of the port fails
        with EINVAL (firmware hung / stale CDC node — the case only a USB
        re-enumerate fixes). A port merely held by our own monitor still opens
        non-exclusively, and a slow-but-alive device opens too — neither is
        touched. Gated hard: never during a test run, only when the hub slot is
        known, cooldown-limited so it can't loop. Disable with
        MESHTASTIC_MCP_AUTO_UNWEDGE=0.
        """
        from ... import port_recovery
        from . import power, test_runner

        if not self.auto_unwedge or test_runner.is_running():
            return
        loc, hub_port = row.get("hub_location"), row.get("hub_port")
        port = row.get("current_port")
        if loc is None or hub_port is None or not port:
            return
        now = time.monotonic()
        if self._unwedge_at.get(serial, 0.0) > now:
            return
        ok, exc = await asyncio.to_thread(port_recovery.port_openable, port, exclusive=False)
        if ok or getattr(exc, "errno", None) != 22:  # not a wedge (EINVAL)
            return
        self._unwedge_at[serial] = now + UNWEDGE_COOLDOWN_S
        log.warning(
            "auto-unwedge: %s (%s) port %s is wedged (%r) — power-cycling hub %s:%s",
            serial,
            row.get("hw_model"),
            port,
            exc,
            loc,
            hub_port,
        )
        try:
            await asyncio.to_thread(power.power_slot, loc, int(hub_port), "cycle")
        except Exception as exc2:
            log.debug("auto-unwedge power-cycle of %s failed: %s", serial, exc2)
            return
        # Re-enumeration follows; the next scan re-binds the device on its new
        # port. Surface a notice so the UI shows the self-heal happened.
        await self.hub.publish(
            "device.update",
            {**row, "note": f"auto-unwedged: power-cycled hub {loc}:{hub_port}"},
        )

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("discovery loop error: %s", exc)
            await asyncio.sleep(POLL_SECONDS)
