"""Escalating device-recovery ladder — the troubleshooting tricks, in one place.

Shared by FleetSuite's runtime (the Recover action + opt-in auto-heal) and the
pytest recovery tier, so both heal a wedged node the same way. Each step is an
independently-callable technique; :func:`run_ladder` escalates through them,
checking device health after each and stopping as soon as the node answers.

Ladder, cheapest/safest first::

    reboot         admin soft reset — transient hangs
    power_cycle    uhubctl VBUS off→on — a wedged USB CDC a soft reset can't reach
    touch_1200bps  1200bps touch → bootloader (preps a frozen device for reflash)
    reflash        rebuild + upload firmware — corrupt firmware
    factory_reset  wipe config — a bad-config boot loop

``SAFE_LADDER`` (reboot → power_cycle) is non-destructive and the default for
unattended/auto recovery; the destructive tail (reflash/factory_reset) is opt-in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("meshtastic_mcp.recovery")

LADDER = ("reboot", "power_cycle", "touch_1200bps", "reflash", "factory_reset")
SAFE_LADDER = ("reboot", "power_cycle")

# Human labels for progress/reporting.
STEP_LABELS = {
    "reboot": "soft reboot",
    "power_cycle": "USB power-cycle",
    "touch_1200bps": "1200bps → bootloader",
    "reflash": "reflash firmware",
    "factory_reset": "factory reset",
}


# --- individual techniques (each independently callable) -------------------
def step_reboot(port: str) -> dict[str, Any]:
    from . import admin

    return admin.reboot(port=port, confirm=True, seconds=2)


def step_power_cycle(location: str, port: int, *, delay_s: int = 3) -> dict[str, Any]:
    from . import uhubctl

    return uhubctl.cycle(location, int(port), delay_s=delay_s)


def step_touch_1200bps(port: str) -> dict[str, Any]:
    from . import flash

    return flash.touch_1200bps(port)


def step_reflash(env: str, port: str) -> dict[str, Any]:
    from . import flash

    return flash.flash(env, port, confirm=True)


def step_factory_reset(port: str) -> dict[str, Any]:
    from . import admin

    return admin.factory_reset(port=port, confirm=True)


# --- health probe ----------------------------------------------------------
def is_healthy(
    port: str | None,
    *,
    timeout_s: float = 20.0,
    resolve_port: Callable[[], str | None] | None = None,
) -> tuple[bool, Any]:
    """Poll ``device_info`` until the node answers with a firmware version — proof
    it booted and its CDC is alive — or the timeout elapses. ``resolve_port``
    re-finds the port after a re-enumeration (power-cycle / bootloader touch)."""
    from . import info

    deadline = time.monotonic() + timeout_s
    detail: Any = None
    while time.monotonic() < deadline:
        p = (resolve_port() if resolve_port else None) or port
        if p:
            try:
                di = info.device_info(port=p, timeout_s=6.0)
                if di.get("firmware_version"):
                    return True, di
                detail = "connected but no firmware_version"
            except Exception as exc:
                detail = str(exc)
        time.sleep(1.0)
    return False, detail


# --- orchestration ---------------------------------------------------------
def run_ladder(
    *,
    port: str | None,
    env: str | None = None,
    hub_location: str | None = None,
    hub_port: int | None = None,
    steps: tuple[str, ...] = SAFE_LADDER,
    health_timeout_s: float = 20.0,
    resolve_port: Callable[[], str | None] | None = None,
    on_step: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Escalate through ``steps``, stopping as soon as the device is healthy.

    Steps missing their prerequisite are skipped (``power_cycle`` without a hub
    slot, ``reflash`` without an env). Returns
    ``{recovered, final_step, steps:[{step, label, skipped, result, healthy_after}]}``.
    Blocking — callers run it in a thread.
    """
    report: dict[str, Any] = {"recovered": False, "final_step": None, "steps": []}

    healthy, _ = is_healthy(port, timeout_s=3.0, resolve_port=resolve_port)
    if healthy:
        report.update(recovered=True, final_step="none")
        return report

    def _cur() -> str | None:
        return (resolve_port() if resolve_port else None) or port

    for step in steps:
        skipped: str | None = None
        result: Any = None
        try:
            if step == "reboot":
                result = step_reboot(_cur())
            elif step == "power_cycle":
                if hub_location is None or hub_port is None:
                    skipped = "no uhubctl hub port mapped"
                else:
                    result = step_power_cycle(hub_location, hub_port)
            elif step == "touch_1200bps":
                result = step_touch_1200bps(_cur())
            elif step == "reflash":
                if not env:
                    skipped = "no pio env resolved"
                else:
                    result = step_reflash(env, _cur())
            elif step == "factory_reset":
                result = step_factory_reset(_cur())
            else:
                skipped = "unknown step"
        except Exception as exc:
            result = {"error": str(exc)}

        entry: dict[str, Any] = {
            "step": step,
            "label": STEP_LABELS.get(step, step),
            "skipped": skipped,
            "result": result,
        }
        if skipped is None:
            healthy, _ = is_healthy(port, timeout_s=health_timeout_s, resolve_port=resolve_port)
            entry["healthy_after"] = healthy
        else:
            healthy = False
        report["steps"].append(entry)
        if on_step is not None:
            on_step(entry)
        if healthy:
            report.update(recovered=True, final_step=step)
            break

    return report
