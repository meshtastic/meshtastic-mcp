# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The recovery ladder heals a dead node.

A powered-off device is the harness's proxy for a wedged one — a soft reboot
can't reach it, so the ladder must escalate to a USB power-cycle to bring it
back. Verifies the escalation actually recovers the node AND preserves its
identity (a hard reset, not a reflash). Parametrized over every connected role.
"""

from __future__ import annotations

import time

import pytest

from meshtastic_mcp import info, recovery, uhubctl
from tests import _power, _recovery
from tests._port_discovery import resolve_port_by_role


# The ladder body — heal (reboot attempt → power_cycle) → re-resolve → identity
# handshake — has never actually executed on this bench: every run died at the
# absence check before reaching it, so 180 s was never a measured budget.
@pytest.mark.timeout(360)
def test_ladder_recovers_a_dead_node(baked_single: dict[str, object]) -> None:
    role = baked_single["role"]
    port = baked_single["port"]
    node_num = baked_single["my_node_num"]

    healthy, detail = recovery.is_healthy(port, timeout_s=5.0)
    assert healthy, f"{role} not healthy before test: {detail}"

    # Resolve the hub slot ONCE, up front. Env pins (seeded by
    # conftest.pytest_configure) resolve even a powered-off board, but resolving
    # here keeps one authority for both the cut and the restore.
    slot = uhubctl.resolve_target(role)

    # Wedge it: cut power so a soft reboot can't reach it.
    _power.power_off(role, resolved=slot)
    # Absence MUST come from the hub's connect flag for this slot. The legacy
    # OS-enumeration check is unusable here twice over: macOS retains a zombie of
    # a powered-off device indefinitely, AND the VID-any predicate is
    # unsatisfiable for the nRF52 roles — t_echo, heltec_t114 and rak4631 all
    # report 0x239a, so cutting one leaves two siblings enumerated and the
    # "no device with this VID anywhere" condition can never become true.
    _power.wait_for_absence(role, timeout_s=10.0, resolved=slot)
    try:
        # reboot fails (it's gone) → power_cycle revives it.
        report = _recovery.heal(port, role=role)
    finally:
        _power.power_on(role, resolved=slot)  # never leave it dark for the next test
        # require_openable: the hub flag flips back the moment VBUS returns, but
        # an nRF52 CDC can re-enumerate on a path that lists and then vanishes.
        # Don't hand the next test a port that won't open.
        resolve_port_by_role(role, timeout_s=30.0, require_openable=True)

    assert report["recovered"], f"ladder did not recover {role}: {report}"
    assert report["final_step"] in ("power_cycle", "reappeared"), report
    steps = {s["step"]: s for s in report["steps"]}
    assert "power_cycle" in steps, report
    assert steps["power_cycle"].get("healthy_after"), (
        f"power_cycle ran but the node didn't come back healthy: {report}"
    )

    # Identity preserved — a hard reset, not a re-flash.
    new_port = resolve_port_by_role(role, timeout_s=30.0, require_openable=True)
    time.sleep(2.0)
    post = info.device_info(port=new_port, timeout_s=10.0)
    assert post.get("my_node_num") == node_num, (
        f"node identity changed during recovery: {node_num:#x} → {post.get('my_node_num')}"
    )


@pytest.mark.timeout(60)
def test_heal_is_a_noop_on_a_healthy_node(baked_single: dict[str, object]) -> None:
    """Recovery on an already-healthy node does nothing — it must not reboot or
    power-cycle a node that's fine."""
    report = _recovery.heal(baked_single["port"], role=baked_single["role"])
    assert report["recovered"] and report["final_step"] == "none"
    assert report["steps"] == []
