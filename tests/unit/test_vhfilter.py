# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the Windows USB power backend (`vhfilter`).

The fixture below is real `vhfilter --list-hubs` output from a bench
machine, not a hand-written sample, because the two things this module can
get wrong are both properties of real topologies rather than of the line
format:

1. A USB 3.x hub enumerates twice, as a SuperSpeed hub and a high-speed
   hub. VirtualHere's guidance is to switch the SuperSpeed one, since that
   takes its companion down with it and not the other way around. Every
   Meshtastic node is a full/high-speed CDC device, so the naive target is
   always the USB2 half — switch that alone and VBUS can stay up, giving a
   power cycle that silently does nothing.

2. The pairing cannot be done by comparing parents. On the 7-port RSH-A37S
   in this fixture two hub chips are cascaded, and the second stage has its
   USB3 half parented to the first stage's USB3 half and its USB2 half to
   the USB2 half — so the two halves of one chip never share a parent, only
   a physical position. Windows' ContainerID does not rescue this either:
   every Realtek hub here reports the generic catch-all GUID, including
   hubs belonging to different physical devices.

Everything under test is pure, so these run on any platform; only
`usb_attachments` needs Windows and it is injected here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from meshtastic_mcp import vhfilter

ROOT = "USB\\ROOT_HUB30\\5&201a262c&0&0"

# A 7-port Rosonway RSH-A37S (two cascaded Realtek chips, 0BDA:0411 SuperSpeed
# + 0BDA:5411 high-speed) and an Elgato Stream Deck dock chain, verbatim.
LIST_HUBS = "\n".join(
    [
        "The following hubs are attached that support Per-Port-Power-Switching:",
        "",
        "USB\\VID_0BDA&PID_5411\\9&1e075a4a&0&1\t has  4 ports\tis USB 2\tand attached to"
        " port  1 on USB\\VID_0BDA&PID_5411\\8&74fe64c&0&1 with companion port  0",
        "USB\\VID_0FD9&PID_00A4\\7&226f78a6&0&1\t has  4 ports\tis USB 2\tand attached to"
        " port  1 on USB\\VID_0FD9&PID_00A4\\6&28cf390b&0&7 with companion port  0",
        "USB\\VID_0FD9&PID_00A4\\6&28cf390b&0&7\t has  5 ports\tis USB 2\tand attached to"
        f" port  7 on {ROOT} with companion port  3",
        "USB\\VID_0BDA&PID_5411\\8&74fe64c&0&1\t has  4 ports\tis USB 2\tand attached to"
        " port  1 on USB\\VID_0FD9&PID_00A4\\7&226f78a6&0&1 with companion port  0",
        "USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2\t has  4 ports\tis USB 3\tand attached to"
        f" port  2 on {ROOT} with companion port  6",
        "USB\\VID_0BDA&PID_0411\\7&202a9e53&0&4\t has  4 ports\tis USB 3\tand attached to"
        " port  4 on USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2 with companion port  4",
        "USB\\VID_0BDA&PID_5411\\6&28cf390b&0&6\t has  4 ports\tis USB 2\tand attached to"
        f" port  6 on {ROOT} with companion port  2",
        "USB\\VID_0BDA&PID_5411\\7&91a51c4&0&4\t has  4 ports\tis USB 2\tand attached to"
        " port  4 on USB\\VID_0BDA&PID_5411\\6&28cf390b&0&6 with companion port  4",
    ]
)

# Stage 1 of the A37S.
SS_STAGE1 = "USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2"
HS_STAGE1 = "USB\\VID_0BDA&PID_5411\\6&28cf390b&0&6"
# Stage 2, cascaded off stage 1.
SS_STAGE2 = "USB\\VID_0BDA&PID_0411\\7&202a9e53&0&4"
HS_STAGE2 = "USB\\VID_0BDA&PID_5411\\7&91a51c4&0&4"


def _result(stdout: str = "", exit_code: int = 0) -> dict:
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": "",
        "stdout_tail": stdout,
        "stderr_tail": "",
        "duration_s": 0.1,
    }


# --- parsing ----------------------------------------------------------------


def test_parse_list_hubs_reads_every_hub() -> None:
    hubs = vhfilter.parse_list_hubs(LIST_HUBS)
    assert len(hubs) == 8
    by_location = {h["location"]: h for h in hubs}

    stage1 = by_location[SS_STAGE1]
    assert stage1 == {
        "location": SS_STAGE1,
        "vid": 0x0BDA,
        "pid": 0x0411,
        "port_count": 4,
        "usb_version": 3,
        "upstream_port": 2,
        "parent": ROOT,
        "companion_port": 6,
    }


def test_parse_list_hubs_ignores_the_banner_and_blank_lines() -> None:
    assert (
        vhfilter.parse_list_hubs(
            "The following hubs are attached that support Per-Port-Power-Switching:\n\n\n"
        )
        == []
    )


# --- physical pairing -------------------------------------------------------


def test_physical_paths_pair_the_two_halves_of_each_chip() -> None:
    paths = vhfilter.physical_paths(vhfilter.parse_list_hubs(LIST_HUBS))

    assert paths[SS_STAGE1] == paths[HS_STAGE1]
    assert paths[SS_STAGE2] == paths[HS_STAGE2]
    # The cascade is a distinct chip from the hub it hangs off.
    assert paths[SS_STAGE1] != paths[SS_STAGE2]


def test_physical_paths_keep_unrelated_hubs_apart() -> None:
    """The Elgato chain must not collide with the A37S, or a power cycle
    would land on somebody else's hub."""
    paths = vhfilter.physical_paths(vhfilter.parse_list_hubs(LIST_HUBS))
    elgato = [loc for loc in paths if "0FD9" in loc or "8&74fe64c" in loc or "9&1e075a4a" in loc]
    assert len(elgato) == 4
    assert len({paths[loc] for loc in elgato}) == 4


def test_physical_paths_survive_a_parent_cycle() -> None:
    """A malformed listing must not hang the resolver."""
    hubs = [
        {
            "location": "A",
            "parent": "B",
            "upstream_port": 1,
            "companion_port": 0,
            "usb_version": 2,
            "port_count": 4,
        },
        {
            "location": "B",
            "parent": "A",
            "upstream_port": 1,
            "companion_port": 0,
            "usb_version": 2,
            "port_count": 4,
        },
    ]
    assert len(vhfilter.physical_paths(hubs)) == 2


# --- switch targeting -------------------------------------------------------


def test_switch_target_redirects_a_high_speed_hub_to_its_superspeed_twin() -> None:
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        target, redirected_from = vhfilter._switch_target(HS_STAGE1, 3)
    assert target == SS_STAGE1
    assert redirected_from == HS_STAGE1


def test_switch_target_redirects_across_a_cascade_stage() -> None:
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        target, _ = vhfilter._switch_target(HS_STAGE2, 1)
    assert target == SS_STAGE2


def test_switch_target_leaves_a_superspeed_hub_alone() -> None:
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        target, redirected_from = vhfilter._switch_target(SS_STAGE1, 3)
    assert target == SS_STAGE1
    assert redirected_from is None


def test_switch_target_matches_location_case_insensitively() -> None:
    """Windows returns the same devnode with different suffix casing
    depending on which API you ask, so an exact match finds nothing."""
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        target, _ = vhfilter._switch_target(HS_STAGE1.upper(), 3)
    assert target == SS_STAGE1


def test_switch_target_keeps_the_hub_when_the_twin_is_too_narrow() -> None:
    """Never silently switch a port number the SuperSpeed half does not have."""
    narrow = LIST_HUBS.replace(
        "USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2\t has  4 ports",
        "USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2\t has  2 ports",
    )
    with patch.object(vhfilter, "_run", return_value=_result(narrow)):
        target, redirected_from = vhfilter._switch_target(HS_STAGE1, 4)
    assert target == HS_STAGE1
    assert redirected_from is None


def test_switch_target_rejects_an_unknown_hub() -> None:
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        with pytest.raises(vhfilter.VhfilterError, match="not in the vhfilter listing"):
            vhfilter._switch_target("USB\\VID_DEAD&PID_BEEF\\1&2&3", 1)


# --- switch / cycle ---------------------------------------------------------


def test_switch_port_invokes_vhfilter_with_the_redirected_hub() -> None:
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=30.0):
        calls.append(args)
        return _result(LIST_HUBS if args == ["--list-hubs"] else "")

    with patch.object(vhfilter, "_run", side_effect=fake_run):
        out = vhfilter.switch_port(HS_STAGE1, 3, on=False)

    assert ["--switch-port", "3", "off", SS_STAGE1] in calls
    # The caller's own location is echoed back, so round-tripping a target
    # through power_off/power_on stays stable.
    assert out["location"] == HS_STAGE1
    assert out["switched_hub"] == SS_STAGE1
    assert out["action"] == "off"


def test_cycle_drives_off_then_on() -> None:
    states: list[str] = []

    def fake_run(args, *, timeout=30.0):
        if args == ["--list-hubs"]:
            return _result(LIST_HUBS)
        states.append(args[2])
        return _result("")

    with patch.object(vhfilter, "_run", side_effect=fake_run):
        with patch.object(vhfilter.time, "sleep") as sleep:
            out = vhfilter.cycle(SS_STAGE1, 2, delay_s=3)

    assert states == ["off", "on"]
    sleep.assert_called_once_with(3)
    assert out["action"] == "cycle"
    assert out["delay_s"] == 3


def test_switch_port_raises_on_a_nonzero_exit() -> None:
    def fake_run(args, *, timeout=30.0):
        if args == ["--list-hubs"]:
            return _result(LIST_HUBS)
        return _result("", exit_code=1)

    with patch.object(vhfilter, "_run", side_effect=fake_run):
        with pytest.raises(vhfilter.VhfilterError, match="failed"):
            vhfilter.switch_port(SS_STAGE1, 1, on=True)


# --- the uhubctl-shaped listing --------------------------------------------


def test_list_hubs_joins_attachments_case_insensitively() -> None:
    """The PnP tree reports a parent's id upper-cased while vhfilter prints
    it lower-cased; joining these by exact string finds nothing at all."""
    attachments = {
        (SS_STAGE1.upper(), 2): {
            "device_vid": 0x239A,
            "device_pid": 0x8029,
            "device_desc": "USB\\VID_239A&PID_8029\\ABC",
        }
    }
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        with patch.object(vhfilter, "usb_attachments", return_value=attachments):
            hubs = vhfilter.list_hubs()

    hub = next(h for h in hubs if h["location"] == SS_STAGE1)
    assert len(hub["ports"]) == 4
    assert hub["ppps"] is True
    populated = [p for p in hub["ports"] if p["device_vid"] is not None]
    assert [p["port"] for p in populated] == [2]
    assert populated[0]["device_vid"] == 0x239A
    assert "connect" in populated[0]["flags"]
    assert "connect" not in hub["ports"][0]["flags"]


def test_list_hubs_is_consumable_by_the_uhubctl_helpers() -> None:
    """`find_port_for_vid` and `device_on_port` are written against the
    uhubctl record shape; this pins that the Windows one satisfies them."""
    from meshtastic_mcp import uhubctl

    attachments = {
        (SS_STAGE2.upper(), 3): {
            "device_vid": 0x303A,
            "device_pid": 0x1001,
            "device_desc": "USB\\VID_303A&PID_1001\\XYZ",
        }
    }
    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        with patch.object(vhfilter, "usb_attachments", return_value=attachments):
            with patch.object(uhubctl, "_IS_WINDOWS", True):
                assert uhubctl.find_port_for_vid(0x303A) == [(SS_STAGE2, 3)]
                assert uhubctl.device_on_port(SS_STAGE2, 3) is True
                assert uhubctl.device_on_port(SS_STAGE2, 1) is False
                with pytest.raises(uhubctl.UhubctlError, match="no port 9"):
                    uhubctl.device_on_port(SS_STAGE2, 9)


# --- enumeration failures must not read as "device absent" -----------------


class _FakeCfgmgr:
    """Minimal cfgmgr32 stand-in driven by a scripted CONFIGRET sequence."""

    def __init__(self, size_rc=0, list_rcs=(0,), ids=(r"USB\VID_1234&PID_5678\AAA",)):
        self.size_rc = size_rc
        self.list_rcs = list(list_rcs)
        self.ids = ids
        self.list_calls = 0

    def CM_Get_Device_ID_List_SizeW(self, size_ptr, flt, flags):
        if self.size_rc == 0:
            size_ptr._obj.value = sum(len(i) + 1 for i in self.ids) + 1
        return self.size_rc

    def CM_Get_Device_ID_ListW(self, flt, buf, size, flags):
        rc = self.list_rcs[min(self.list_calls, len(self.list_rcs) - 1)]
        self.list_calls += 1
        if rc == 0:
            blob = "\0".join(self.ids) + "\0\0"
            buf[: len(blob)] = blob
        return rc


def test_device_id_list_raises_instead_of_reporting_an_empty_bus() -> None:
    """A cfgmgr32 failure must not look like "no USB devices attached".

    `list_hubs` would turn an empty list into ports with no attachment, and
    `device_on_port` reads that as nothing plugged in. A caller polling for
    absence after a power cut would then pass having never seen a
    disconnect. Absence has to be observed, not inferred from a failure.
    """
    with pytest.raises(vhfilter.VhfilterError, match="CM_Get_Device_ID_List_SizeW"):
        vhfilter._device_id_list(_FakeCfgmgr(size_rc=0x16))

    with pytest.raises(vhfilter.VhfilterError, match="CM_Get_Device_ID_ListW"):
        vhfilter._device_id_list(_FakeCfgmgr(list_rcs=(0x16,)))


def test_device_id_list_retries_when_the_buffer_grew_under_it() -> None:
    """The size query and the fetch are not atomic; CR_BUFFER_SMALL means a
    device appeared in between, so re-query rather than return a short list."""
    lib = _FakeCfgmgr(list_rcs=(vhfilter._CR_BUFFER_SMALL, 0))
    assert vhfilter._device_id_list(lib) == [r"USB\VID_1234&PID_5678\AAA"]
    assert lib.list_calls == 2


def test_device_id_list_gives_up_if_it_never_gets_a_stable_snapshot() -> None:
    lib = _FakeCfgmgr(list_rcs=(vhfilter._CR_BUFFER_SMALL,))
    with pytest.raises(vhfilter.VhfilterError, match="grew on every one of"):
        vhfilter._device_id_list(lib)
    assert lib.list_calls == vhfilter._ID_LIST_ATTEMPTS


def test_enumeration_failure_propagates_out_of_device_on_port() -> None:
    """End to end: the raise must reach the caller rather than being
    flattened into False somewhere in the layers above."""
    from meshtastic_mcp import uhubctl

    with patch.object(vhfilter, "_run", return_value=_result(LIST_HUBS)):
        with patch.object(
            vhfilter, "usb_attachments", side_effect=vhfilter.VhfilterError("cfgmgr32 exploded")
        ):
            with patch.object(uhubctl, "_IS_WINDOWS", True):
                with pytest.raises(uhubctl.UhubctlError, match="cfgmgr32 exploded"):
                    uhubctl.device_on_port(SS_STAGE1, 1)


def test_cycle_keeps_an_explicit_zero_delay() -> None:
    """`delay_s=0` is inside the tool surface's documented 0..60 range and
    must not be rewritten to the default."""
    from meshtastic_mcp import uhubctl

    with patch.object(uhubctl, "_IS_WINDOWS", True):
        with patch.object(vhfilter, "cycle", return_value={}) as cyc:
            uhubctl.cycle(SS_STAGE1, 1, delay_s=0)
    assert cyc.call_args.kwargs["delay_s"] == 0
