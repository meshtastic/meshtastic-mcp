"""FleetSuite device recovery.

Drives the shared :mod:`meshtastic_mcp.recovery` ladder against a registry
device — resolving its port, pio env, and uhubctl hub slot from the device row —
under the per-device port guard (so nothing else touches the port mid-recovery),
streaming per-step progress over the ``recovery.update`` WebSocket topic.
"""

from __future__ import annotations

import asyncio
import logging

from meshtastic_mcp import recovery as rec

from ..db import repo_devices as rd
from . import control

log = logging.getLogger("meshtastic_mcp.web.recovery")

# How long the post-ladder confirmation probe waits for a device_info handshake
# on a re-enumerated device before giving up on the "reappeared" promotion.
REAPPEAR_HEALTH_TIMEOUT_S = 15.0

# Ladder steps that rewrite flash or wipe config. Per the repo convention for
# destructive tools (reboot/factory_reset/erase_and_flash/uhubctl_*), these
# require an explicit confirm=True from the caller.
DESTRUCTIVE_STEPS = frozenset({"reflash", "factory_reset", "erase_and_flash"})


class RecoveryService:
    def __init__(self, db, hub, serialmon=None, portlocks=None) -> None:
        self.db = db
        self.hub = hub
        from .portlock import PortLocks

        self.portlocks = portlocks or PortLocks(serialmon)
        self._active: set[str] = set()

    def is_recovering(self, serial: str) -> bool:
        return serial in self._active

    async def recover(
        self, serial: str, *, allow_reflash: bool = False, confirm: bool = False, steps=None
    ) -> dict:
        device = await rd.get(self.db, serial)
        if device is None:
            raise LookupError(serial)
        if device.get("kind") == "native":
            raise RuntimeError("native (TCP) nodes can't be hardware-recovered")
        if serial in self._active:
            raise RuntimeError("recovery already in progress for this device")

        if steps is None:
            ladder = list(rec.SAFE_LADDER)
            if allow_reflash:
                ladder += ["touch_1200bps", "reflash"]
        else:
            ladder = list(steps)

        destructive = DESTRUCTIVE_STEPS.intersection(ladder)
        if destructive and not confirm:
            raise RuntimeError(
                f"destructive recovery steps {sorted(destructive)} require confirm=True"
            )
        if "reflash" in destructive and not allow_reflash:
            raise RuntimeError("the reflash step requires allow_reflash=True")

        port = device.get("current_port")
        env = control.env_for_device(device)
        hub_location = device.get("hub_location")
        hub_port = device.get("hub_port")

        self._active.add(serial)
        await self.hub.publish(
            "recovery.update",
            {"serial": serial, "state": "started", "ladder": ladder},
        )
        try:
            # Exclusive device access for the whole sweep; the serial monitor is
            # suspended and enrichment/keep-alive/control wait on the same guard.
            async with self.portlocks.guard(serial):

                def on_step(entry: dict) -> None:
                    self.hub.publish_threadsafe(
                        "recovery.update", {"serial": serial, "state": "step", **entry}
                    )

                report = await asyncio.to_thread(
                    rec.run_ladder,
                    port=port,
                    env=env,
                    hub_location=hub_location,
                    hub_port=hub_port,
                    steps=tuple(ladder),
                    on_step=on_step,
                )

            # The node may have re-enumerated on a new port the run-loop's fixed
            # port couldn't reach; discovery updates the row. But re-enumeration
            # is NOT recovery — a wedged board can sit on the USB bus (online=1)
            # with a dead CDC that never answers, and a failed reflash can leave
            # it exactly there. So gate the "reappeared" promotion on an ACTUAL
            # device_info handshake on the (possibly new) port, not mere
            # enumeration.
            row = await rd.get(self.db, serial)
            if not report["recovered"] and row and row.get("online"):
                probe_port = row.get("current_port") or port
                # Re-enter the guard so the probe's port open doesn't race the
                # resumed serial monitor / enrichment on the same device.
                async with self.portlocks.guard(serial):
                    healthy, detail = await asyncio.to_thread(
                        rec.is_healthy,
                        probe_port,
                        timeout_s=REAPPEAR_HEALTH_TIMEOUT_S,
                    )
                if healthy:
                    report["recovered"] = True
                    report["final_step"] = report.get("final_step") or "reappeared"
                else:
                    # Re-enumerated but unhealthy: surface why, and leave
                    # recovered=False so the caller doesn't trust a dead node.
                    report["reappeared_unhealthy"] = (
                        str(detail)
                        if detail is not None
                        else "re-enumerated but device_info handshake failed"
                    )

            report["serial"] = serial
            await self.hub.publish("recovery.update", {"serial": serial, "state": "done", **report})
            if row:
                await self.hub.publish("device.update", row)
            return report
        finally:
            self._active.discard(serial)
