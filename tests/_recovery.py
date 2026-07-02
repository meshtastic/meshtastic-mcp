"""Recovery helpers for the test harness.

Thin composition over the shared ``meshtastic_mcp.recovery`` ladder, wired to the
tester's uhubctl topology so a test can heal a wedged node mid-run the same way
FleetSuite does. The default harness ladder is the non-destructive
``SAFE_LADDER`` (reboot → power-cycle); a test opts into reflash/factory_reset
explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from meshtastic_mcp import recovery, uhubctl

from ._port_discovery import resolve_port_by_role


def hub_slot_for_role(role: str) -> tuple[str | None, int | None]:
    """The (uhubctl location, port) hosting a role, or (None, None) if it isn't
    on a controllable PPPS hub / can't be resolved."""
    try:
        return uhubctl.resolve_target(role)
    except uhubctl.UhubctlError:
        return None, None


def port_resolver_for_role(role: str) -> Callable[[], str | None]:
    """A re-resolver: after a power cycle a node can re-enumerate on a new path,
    so re-find it by the role's pinned hub slot (VID alone is ambiguous with
    three same-VID nRF52 boards on the bench)."""

    def resolve() -> str | None:
        try:
            return resolve_port_by_role(role, timeout_s=5.0)
        except (AssertionError, ValueError):
            return None

    return resolve


def heal(
    port: str,
    *,
    role: str | None = None,
    env: str | None = None,
    steps: tuple[str, ...] = recovery.SAFE_LADDER,
    health_timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run the recovery ladder against a device, resolving its hub slot + a
    port re-resolver from its role. Returns the recovery report."""
    loc: str | None = None
    hub_port: int | None = None
    resolve = None
    if role:
        loc, hub_port = hub_slot_for_role(role)
        resolve = port_resolver_for_role(role)
    return recovery.run_ladder(
        port=port,
        env=env,
        hub_location=loc,
        hub_port=hub_port,
        steps=steps,
        resolve_port=resolve,
        health_timeout_s=health_timeout_s,
    )
