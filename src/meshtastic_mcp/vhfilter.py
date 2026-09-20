# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Windows backend for USB hub per-port power control, via VirtualHere's
`vhfilter.exe`.

Why this exists: `uhubctl` cannot work on Windows, and this is structural,
not a packaging gap. libusb reaches devices through `winusb.sys`, which
refuses the hub-class control requests that `SET_FEATURE(PORT_POWER)`
needs; libusb closed that as wontfix (libusb#391), swapping in libusbK /
libusb-win32 via Zadig fails identically, the usbdk backend was tried and
failed (uhubctl#69), and `usbipd-win` cannot hand a hub to WSL because the
USBIP protocol has no concept of sharing a hub (usbipd-win#747). What does
work is a kernel upper-filter on the hub class, which is what VirtualHere
ships as a signed generic driver — it drives PPPS on any compliant hub,
not just their own.

Two sources are joined here, because `vhfilter --list-hubs` reports the
hubs but not what is plugged into them:

- `vhfilter --list-hubs` gives the PPPS-capable hubs, their port counts,
  their USB generation, and how each attaches upstream.
- `cfgmgr32` (via ctypes, no dependency) walks the PnP tree for the
  per-port device attachments: `CM_Get_Parent` gives the hub a device
  hangs off, `CM_DRP_ADDRESS` gives the port number on it.

The join is case-insensitive on purpose. Windows is inconsistent about
the case of a device-instance suffix: `CM_Get_Device_IDW` returns a
child's own id as `...\\6&28cf390b&0&2` but the same node as somebody's
parent as `...\\6&28CF390B&0&2`, and `vhfilter` prints it lowercased
again. Matching these by exact string silently finds nothing.

`location` in this module is the hub's PnP device path, e.g.
`USB\\VID_0BDA&PID_0411\\6&28cf390b&0&2`. That is what `vhfilter` wants
as its `devicePath` argument, so it is what callers get back and pass in,
in place of uhubctl's `1-1.3` notation.

Presence semantics differ from uhubctl and callers should know it: uhubctl
reads the connect bit out of the hub's own port-status register, whereas
here presence comes from the OS PnP tree. Windows tears a devnode down off
the hub's connect-change interrupt, so it is prompt, but it is still the
OS's view rather than the hub's.

Hardware gotchas, both of which make a switch report success and do
nothing, and neither of which the hub will tell us about:

- Hubs with per-port mechanical switches (the Rosonway RSH-A37S this was
  developed against has seven latching ones) wire them in series with the
  controller's power switching. A disengaged latch holds the port dead no
  matter what PPPS is told, so `power_on` returns success and the device
  never comes back. If a port will not wake, check the physical switch
  before suspecting this code.
- USB Selective Suspend powers an idle hub down entirely, and it then
  never sees the request; that one at least surfaces as error 0x000003e3
  and is translated below.
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from ctypes import wintypes
from typing import Any

from . import config, hw_tools

IS_WINDOWS = sys.platform == "win32"


class VhfilterError(RuntimeError):
    """Raised on vhfilter-specific failures: driver not installed, hub or
    port not found, or a switch that the driver refused."""


# ---------- PnP enumeration (cfgmgr32) -------------------------------------

_CR_SUCCESS = 0
_CM_DRP_ADDRESS = 0x1D
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001

_VIDPID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")


def _cfgmgr() -> ctypes.WinDLL:
    if not IS_WINDOWS:
        raise VhfilterError("cfgmgr32 is Windows-only")
    return ctypes.WinDLL("cfgmgr32")


def _device_id_list(lib: ctypes.WinDLL, enumerator: str = "USB") -> list[str]:
    """Every device instance id under the `USB` enumerator."""
    size = wintypes.ULONG()
    flt = ctypes.c_wchar_p(enumerator)
    if (
        lib.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), flt, _CM_GETIDLIST_FILTER_ENUMERATOR)
        != _CR_SUCCESS
    ):
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if (
        lib.CM_Get_Device_ID_ListW(flt, buf, size.value, _CM_GETIDLIST_FILTER_ENUMERATOR)
        != _CR_SUCCESS
    ):
        return []
    # The buffer is a NUL-separated, double-NUL-terminated multi-string, so
    # `buf.value` is no good: it would stop at the first id. Slicing the
    # array yields the whole thing at runtime but is typed as `list[str]`,
    # hence `wstring_at`, which reads a fixed character count and keeps the
    # embedded NULs.
    blob = ctypes.wstring_at(ctypes.addressof(buf), size.value)
    return [s for s in blob.split("\0") if s]


def _devnode(lib: ctypes.WinDLL, device_id: str) -> wintypes.DWORD | None:
    """Locate a devnode. Returns None for a device that is not present, which
    is how absent-but-remembered entries get filtered out."""
    inst = wintypes.DWORD()
    rc = lib.CM_Locate_DevNodeW(ctypes.byref(inst), ctypes.c_wchar_p(device_id), 0)
    return inst if rc == _CR_SUCCESS else None


def _device_id(lib: ctypes.WinDLL, inst: wintypes.DWORD) -> str | None:
    buf = ctypes.create_unicode_buffer(512)
    rc = lib.CM_Get_Device_IDW(inst, buf, 512, 0)
    return buf.value if rc == _CR_SUCCESS else None


def _parent_id(lib: ctypes.WinDLL, inst: wintypes.DWORD) -> str | None:
    parent = wintypes.DWORD()
    rc = lib.CM_Get_Parent(ctypes.byref(parent), inst, 0)
    return _device_id(lib, parent) if rc == _CR_SUCCESS else None


def _address(lib: ctypes.WinDLL, inst: wintypes.DWORD) -> int | None:
    """`CM_DRP_ADDRESS` is the hub port number for a device whose parent is a
    hub. (For a composite interface it is the interface number instead, but
    those hang off the device, not off a hub, so they never join a hub row.)"""
    val = wintypes.DWORD()
    size = wintypes.ULONG(ctypes.sizeof(val))
    rtype = wintypes.ULONG()
    rc = lib.CM_Get_DevNode_Registry_PropertyW(
        inst, _CM_DRP_ADDRESS, ctypes.byref(rtype), ctypes.byref(val), ctypes.byref(size), 0
    )
    return val.value if rc == _CR_SUCCESS else None


def usb_attachments() -> dict[tuple[str, int], dict[str, Any]]:
    """Map `(parent hub path upper-cased, port) -> device record`.

    The key is upper-cased because Windows is not self-consistent about the
    case of an instance-id suffix; see the module docstring.
    """
    lib = _cfgmgr()
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for device_id in _device_id_list(lib):
        inst = _devnode(lib, device_id)
        if inst is None:
            continue
        parent = _parent_id(lib, inst)
        port = _address(lib, inst)
        if parent is None or port is None:
            continue
        match = _VIDPID_RE.search(device_id)
        out[(parent.upper(), port)] = {
            "device_vid": int(match.group(1), 16) if match else None,
            "device_pid": int(match.group(2), 16) if match else None,
            "device_desc": device_id,
        }
    return out


# ---------- vhfilter --list-hubs parsing -----------------------------------

# USB\VID_0BDA&PID_0411\6&28cf390b&0&2	 has  4 ports	is USB 3	and attached to
# port  2 on USB\ROOT_HUB30\5&201a262c&0&0 with companion port  6
_HUB_RE = re.compile(
    r"^(?P<path>\S+)\s+has\s+(?P<ports>\d+)\s+ports?\s+"
    r"is USB\s+(?P<usb_version>\d+)\s+"
    r"and attached to port\s+(?P<upstream_port>\d+)\s+"
    r"on\s+(?P<parent>\S+)\s+"
    r"with companion port\s+(?P<companion_port>\d+)\s*$"
)


def parse_list_hubs(output: str) -> list[dict[str, Any]]:
    """Parse `vhfilter --list-hubs` stdout into raw hub records."""
    hubs: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = _HUB_RE.match(line.strip())
        if not match:
            continue
        path = match.group("path")
        vid_pid = _VIDPID_RE.search(path)
        hubs.append(
            {
                "location": path,
                "vid": int(vid_pid.group(1), 16) if vid_pid else None,
                "pid": int(vid_pid.group(2), 16) if vid_pid else None,
                "port_count": int(match.group("ports")),
                "usb_version": int(match.group("usb_version")),
                "upstream_port": int(match.group("upstream_port")),
                "parent": match.group("parent"),
                "companion_port": int(match.group("companion_port")),
            }
        )
    return hubs


# ---------- Physical-path pairing ------------------------------------------


def _canonical_port(hub: dict[str, Any]) -> int:
    """Collapse a hub's upstream port and its companion into one number, so
    the USB2 and USB3 halves of one chip agree on where they sit.

    A USB 3.x hub enumerates twice — once as a SuperSpeed hub, once as a
    high-speed hub — on two different upstream ports of the parent, each
    naming the other as its companion. Taking the lower of the pair gives
    both halves the same answer. A companion of 0 means there is none.
    """
    companion = hub["companion_port"]
    if companion <= 0:
        return hub["upstream_port"]
    return min(hub["upstream_port"], companion)


def physical_paths(hubs: list[dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    """Map each hub location to a path identifying the physical chip it is.

    The two logical hubs of one chip resolve to an equal path, which is what
    lets `_switch_target` find a USB2 hub's SuperSpeed twin. Walking upwards
    (rather than comparing parents directly) is required for cascaded hubs:
    the second stage of a 7-port hub has its USB3 half parented to the first
    stage's USB3 half and its USB2 half to the USB2 half, so the two halves
    never share a parent, only a physical position.
    """
    by_location = {hub["location"].upper(): hub for hub in hubs}
    cache: dict[str, tuple[Any, ...]] = {}

    def resolve(key: str, seen: frozenset[str]) -> tuple[Any, ...]:
        if key in cache:
            return cache[key]
        hub = by_location[key]
        parent_key = hub["parent"].upper()
        if parent_key in by_location and parent_key not in seen:
            base = resolve(parent_key, seen | {key})
        else:
            # Anchored at whatever is above the last hub we know about — a
            # root hub, or a hub the filter did not report. Keep the raw id
            # so two different controllers never collide.
            base = (parent_key,)
        path = (*base, _canonical_port(hub))
        cache[key] = path
        return path

    return {hub["location"]: resolve(hub["location"].upper(), frozenset()) for hub in hubs}


# ---------- Binary invocation ----------------------------------------------

# vhfilter surfaces a raw Win32 error when the hub will not answer. 0x3E3 is
# the one users actually hit: Windows had suspended the hub (selective
# suspend powers an idle hub down entirely) so it never saw the request.
_SELECTIVE_SUSPEND_HINT = (
    "vhfilter could not reach the hub (error 0x000003e3). This is normally "
    "USB Selective Suspend powering the hub down while idle: Control Panel -> "
    "Power Options -> Change plan settings -> Change advanced power settings -> "
    "USB settings -> USB selective suspend setting -> Disabled."
)

_NOT_INSTALLED_PATTERNS = (
    "filter is not installed",
    "not installed",
    "no filter",
)


def _run(args: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
    binary = config.vhfilter_bin()
    result = hw_tools._run(binary, args, timeout=timeout)
    combined = ((result.get("stderr") or "") + "\n" + (result.get("stdout") or "")).lower()
    if result["exit_code"] != 0:
        if "0x000003e3" in combined or "0x3e3" in combined:
            raise VhfilterError(_SELECTIVE_SUSPEND_HINT)
        if any(pat in combined for pat in _NOT_INSTALLED_PATTERNS):
            raise VhfilterError(
                "the vhfilter hub filter driver is not active. Install it from an "
                "elevated prompt with `vhfilter --install-filter`, then REBOOT — "
                "the filter only attaches to the hub stack at boot."
            )
    return result


def available() -> bool:
    """True when the vhfilter binary can be resolved. Says nothing about
    whether the filter driver itself is installed and rebooted into."""
    if not IS_WINDOWS:
        return False
    try:
        config.vhfilter_bin()
    except Exception:
        return False
    return True


# ---------- Public API (mirrors uhubctl's shapes) --------------------------


def list_hubs() -> list[dict[str, Any]]:
    """Enumerate PPPS-capable hubs with their per-port device attachments.

    Returns the same record shape as `uhubctl.parse_list_output`, so the
    callers in `uhubctl` work unchanged. Every hub vhfilter lists is PPPS
    capable by construction — it only prints hubs that support it — so
    `ppps` is always True here.

    `status` is empty and `flags` is synthesised: reading a hub's real
    port-status register is exactly the thing Windows will not let a
    user-mode caller do, which is why this backend exists at all.
    """
    result = _run(["--list-hubs"], timeout=20.0)
    if result["exit_code"] != 0:
        raise VhfilterError(
            f"vhfilter --list-hubs failed (exit {result['exit_code']}): "
            f"{result.get('stderr_tail')!r}"
        )

    raw = parse_list_hubs(result["stdout"])
    attachments = usb_attachments()

    hubs: list[dict[str, Any]] = []
    for hub in raw:
        key = hub["location"].upper()
        ports = []
        for port_number in range(1, hub["port_count"] + 1):
            attached = attachments.get((key, port_number))
            ports.append(
                {
                    "port": port_number,
                    "status": "",
                    "flags": "power connect" if attached else "power",
                    "device_vid": attached["device_vid"] if attached else None,
                    "device_pid": attached["device_pid"] if attached else None,
                    "device_desc": attached["device_desc"] if attached else None,
                }
            )
        hubs.append(
            {
                "location": hub["location"],
                "descriptor": (
                    f"{hub['vid']:04x}:{hub['pid']:04x} USB {hub['usb_version']}.0, "
                    f"{hub['port_count']} ports, ppps"
                    if hub["vid"] is not None
                    else f"USB {hub['usb_version']}.0, {hub['port_count']} ports, ppps"
                ),
                "vid": hub["vid"],
                "pid": hub["pid"],
                "ppps": True,
                "ports": ports,
                "usb_version": hub["usb_version"],
            }
        )
    return hubs


def _switch_target(location: str, port: int) -> tuple[str, str | None]:
    """Pick which hub to actually switch, preferring the SuperSpeed twin.

    VirtualHere's guidance is to switch the USB3 port because that takes its
    USB2 companion down with it; the reverse does not hold. Meshtastic nodes
    are all full/high-speed CDC devices, so the naive target is always the
    USB2 half, and switching it alone can leave VBUS up — a power cycle that
    silently does nothing. Returns `(target, redirected_from)`.

    ponytail: assumes port N on the SuperSpeed half is the same receptacle as
    port N on the high-speed half, which is how compliant USB 3.x hubs are
    numbered. If a hub is ever found that renumbers between halves, pin the
    port explicitly with the MESHTASTIC_UHUBCTL_LOCATION_/_PORT_ env vars.
    """
    hubs = parse_list_hubs(_run(["--list-hubs"], timeout=20.0)["stdout"])
    by_location = {hub["location"].upper(): hub for hub in hubs}
    current = by_location.get(location.upper())
    if current is None:
        raise VhfilterError(
            f"hub {location!r} is not in the vhfilter listing. Run "
            f"`vhfilter --list-hubs` to see the PPPS-capable hubs."
        )
    if current["usb_version"] >= 3:
        return current["location"], None

    paths = physical_paths(hubs)
    mine = paths[current["location"]]
    for hub in hubs:
        if hub["usb_version"] >= 3 and paths[hub["location"]] == mine:
            if port <= hub["port_count"]:
                return hub["location"], current["location"]
            break
    return current["location"], None


def switch_port(location: str, port: int, on: bool) -> dict[str, Any]:
    """Drive a single port's VBUS. `location` is the hub's PnP device path.

    A success here means the hub accepted the request, not that the port
    changed state: a disengaged mechanical port switch overrides PPPS
    silently. Callers that need to know a device actually came back should
    poll `device_on_port` rather than trust this return.
    """
    target, redirected_from = _switch_target(location, port)
    state = "on" if on else "off"
    result = _run(["--switch-port", str(port), state, target], timeout=30.0)
    if result["exit_code"] != 0:
        raise VhfilterError(
            f"vhfilter --switch-port {port} {state} {target} failed "
            f"(exit {result['exit_code']}): {result.get('stderr_tail')!r}"
        )
    out: dict[str, Any] = {
        "action": state,
        "location": location,
        "port": port,
        # uhubctl's `_action` always carries this, None for a plain on/off.
        # Keep the key so a caller can read either backend's result blind.
        "delay_s": None,
        "duration_s": result["duration_s"],
    }
    if redirected_from is not None:
        out["switched_hub"] = target
        out["redirected_from"] = redirected_from
    return out


def cycle(location: str, port: int, delay_s: int = 2) -> dict[str, Any]:
    """Off, wait, on. vhfilter has no cycle verb, so the delay is ours."""
    off = switch_port(location, port, on=False)
    time.sleep(delay_s)
    on = switch_port(location, port, on=True)
    return {
        "action": "cycle",
        "location": location,
        "port": port,
        "delay_s": delay_s,
        "duration_s": off["duration_s"] + delay_s + on["duration_s"],
        **{k: v for k, v in on.items() if k in ("switched_hub", "redirected_from")},
    }


__all__ = [
    "IS_WINDOWS",
    "VhfilterError",
    "available",
    "cycle",
    "list_hubs",
    "parse_list_hubs",
    "physical_paths",
    "switch_port",
    "usb_attachments",
]
