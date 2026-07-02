"""Full power-cycle round-trip: off → verify gone → on → verify identity
preserved.

Parametrized over every connected role. Validates both the uhubctl
plumbing AND that the device survives a hard reset with the same
`my_node_num` (no firmware-level identity regeneration).
"""

from __future__ import annotations

import sys
import time

import pytest

from meshtastic_mcp import info, port_recovery
from tests import _power
from tests._port_discovery import resolve_port_by_role


# Generous timeout: under a doubly-bad bench run the body AND the finally can each
# enter deep recovery (a bounded power_off re-issue, plus ensure_port_responsive
# which may power-cycle a node that comes back wedged), which stacks to a few
# minutes. The happy path is still ~10s. We also cap the inner recovery budgets
# (reenum/health, below) so the worst case stays well under this ceiling rather
# than letting a mid-finally SIGALRM replace the real verdict with a timeout.
@pytest.mark.timeout(480)
def test_power_cycle_preserves_node_identity(
    baked_single: dict[str, object],
) -> None:
    role = baked_single["role"]
    pre_port = baked_single["port"]
    pre_node_num = baked_single["my_node_num"]
    pre_fw = baked_single.get("firmware_version")

    # Record pre-cycle state.
    pre_info = info.device_info(port=pre_port, timeout_s=5.0)
    assert pre_info.get("my_node_num") == pre_node_num

    # Resolve the hub slot ONCE, while the device is still visible — once it's
    # powered off, resolve_target can't find it (its VID is gone from every hub)
    # and would raise UhubctlError. Reuse the slot for both off and on.
    from meshtastic_mcp import uhubctl

    slot = uhubctl.resolve_target(role)

    # The pre_info handshake (and baked_single's earlier probe) can leave a serial
    # fd open on a daemon close-thread (connection._close_bounded abandons a slow
    # close after 5s). On macOS a held fd pins the CDC node in the IORegistry, so
    # a VBUS cut would NOT remove the device from list_devices until that fd
    # closes — which is what made wait_for_absence time out. Drain it (and clear
    # any leaked in-process port lock) BEFORE cutting power.
    _power.drain_port_fd(pre_port, timeout_s=8.0)

    try:
        # Power off; confirm THIS device's node disappears (match the exact path,
        # not just the VID, so a second same-VID adapter can't spoof presence).
        _power.power_off(role, resolved=slot)
        try:
            _power.wait_for_absence(role, timeout_s=20.0, expected_port=pre_port)
        except TimeoutError:
            # A racy/incomplete first VBUS cut — re-issue once before giving up.
            _power.power_off(role, resolved=slot)
            try:
                _power.wait_for_absence(role, timeout_s=15.0, expected_port=pre_port)
            except TimeoutError:
                pytest.fail(
                    f"device {role!r} (port {pre_port}) stayed visible after power_off (twice)"
                )

        # Power back on + re-discover. ensure_port_responsive re-enumerates a
        # node that comes back openable-but-wedged (EINVAL) so the post handshake
        # below hits a live device, not a stale ghost.
        _power.power_on(role, resolved=slot)
        time.sleep(0.5)  # head-start before polling
        new_port = resolve_port_by_role(role, timeout_s=30.0)
        new_port = port_recovery.ensure_port_responsive(new_port, role=role, reenum_timeout_s=30.0)

        post_info = info.device_info(port=new_port, timeout_s=10.0)
        assert post_info.get("my_node_num") == pre_node_num, (
            f"my_node_num changed across power-cycle: pre={pre_node_num:#x} "
            f"post={post_info.get('my_node_num'):#x}"
        )
        # Firmware version must match (same bake, not a re-flash).
        if pre_fw:
            assert post_info.get("firmware_version") == pre_fw, (
                f"firmware changed across cycle: pre={pre_fw} "
                f"post={post_info.get('firmware_version')}"
            )
    finally:
        # Always leave the bench powered ON + answering for the next tier, no
        # matter which branch above failed (assertion, exception, or pytest.fail).
        # power_on is idempotent; ensure_port_responsive re-enumerates a node that
        # came back wedged. Recovery trouble is logged but must NEVER overwrite the
        # test's real verdict, so it's fully swallowed here.
        try:
            _power.power_on(role, resolved=slot)
            time.sleep(0.5)
            # resolve_port_by_role waits for re-enumeration and finds the device
            # even if it came back on a new /dev path (more robust than a fixed
            # short settle + topology lookup). A wedged-but-enumerated node still
            # carries its VID, so this finds it; ensure_port_responsive then
            # re-enumerates it if it won't answer.
            back = resolve_port_by_role(role, timeout_s=15.0)
            # Tighter recovery budget in the finally: it stacks on top of any
            # recovery the body already did, so keep it bounded — the next tier's
            # baked fixture does a full responsive check anyway.
            port_recovery.ensure_port_responsive(
                back, role=role, reenum_timeout_s=20.0, health_timeout_s=8.0
            )
        except Exception as exc:
            print(
                f"[power-cycle-test] WARNING: could not confirm {role!r} healthy "
                f"on the way out: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
