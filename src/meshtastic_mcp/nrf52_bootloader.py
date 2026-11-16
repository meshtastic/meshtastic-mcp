# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""USB features of the Meshtastic nRF52 bootloader (meshtastic/nRF52_Bootloader).

In UF2 mode the bootloader exposes a GhostFAT drive (`src/usb/uf2/ghostfat.c`) holding
`INFO_UF2.TXT`, `INDEX.HTM` and `CURRENT.UF2`. This module finds those drives, parses
`INFO_UF2.TXT`, ties a drive to a board by USB serial number, backs up `CURRENT.UF2`,
copies a `.uf2` onto the drive and sends the factory-erase UF2.

Pure Python with no MCP imports and no firmware-tree assumptions. Every public function
returns a JSON-friendly dict; expected failures come back as `{"ok": False, "error": ...}`
rather than raising. Destructive functions (`flash_uf2`, `factory_erase`) take no
`confirm` flag - the MCP tool layer gates them.

Getting a board into UF2 mode is the caller's job: double-press reset, or the
`AdminMessage.enter_dfu_mode_request` admin message (`meshtastic --enter-dfu`), which on
nRF52 sets GPREGRET to `DFU_MAGIC_UF2_RESET` (0x57) and resets. The board needs a USB host
within 3 seconds or it boots the application again.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

INFO_FILE = "INFO_UF2.TXT"
CURRENT_FILE = "CURRENT.UF2"
FACTORY_ERASE_UF2_RELPATH = Path("tools") / "meshtastic_factory_erase.uf2"

# UF2 block layout, src/usb/uf2/uf2.h. All fields little endian.
UF2_BLOCK_SIZE = 512
UF2_PAYLOAD_SIZE = 256
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_NOFLASH = 0x00000001
UF2_FLAG_FAMILYID = 0x00002000

# Family ids, src/usb/uf2/uf2cfg.h.
FAMILY_FACTORY_ERASE = 0x4D455348  # "MESH"
FAMILY_BOOTLOADER = 0xD663823C
FAMILY_NRF52840_APP = 0xADA52840
FAMILY_NRF52833_APP = 0x621E937A

_FAMILIES: dict[int, tuple[str, str]] = {
    FAMILY_FACTORY_ERASE: ("factory_erase", "Meshtastic factory erase"),
    FAMILY_BOOTLOADER: ("bootloader_update", "nRF52 bootloader update"),
    FAMILY_NRF52840_APP: ("application", "nRF52840 application"),
    FAMILY_NRF52833_APP: ("application", "nRF52833 application"),
}

# Board-specific application families are (USB VID << 16) | UF2 PID; boards sharing an id
# accept each other's files (README, "Updating the bootloader").
_BOARD_APP_FAMILIES: dict[int, str] = {
    0x239A0029: "RAK4631, RAK3401, T-Echo, WisMesh Tag, MX25LE01, Meshtastic Generic",
    0x239A0071: "Heltec T096, T1, T114",
    0x239A00DA: "ThinkNode M1, M3, M6",
    0x28860044: "SenseCAP Solar Node P1, XIAO nRF52840 BLE",
    0x28860045: "XIAO nRF52840 BLE Sense",
    0x28860057: "T1000-E, MeshTracker X1",
    0x28861667: "Wio Tracker L1, L1 Pro 1W",
}

# A 1 MB nRF52840 image is 2 MB of UF2; anything far past that is not a firmware file.
_MAX_UF2_BYTES = 16 * 1024 * 1024
_FAT_FSTYPES = frozenset({"vfat", "msdos", "fat"})

DrivePath = str | os.PathLike[str] | Mapping[str, Any]


def _hex(value: int) -> str:
    return f"0x{value:08X}"


# --------------------------------------------------------------------------- UF2 files


def family_info(family_id: int) -> dict[str, Any]:
    """Name a UF2 family id the way this bootloader classifies it."""
    if family_id in _FAMILIES:
        kind, name = _FAMILIES[family_id]
    elif family_id in _BOARD_APP_FAMILIES:
        kind = "board_application"
        vid, pid = family_id >> 16, family_id & 0xFFFF
        name = f"board application, USB {vid:04X}:{pid:04X} ({_BOARD_APP_FAMILIES[family_id]})"
    else:
        kind, name = "unknown", "unknown family"
    return {"family_id": _hex(family_id), "kind": kind, "name": name}


def factory_erase_uf2_bytes() -> bytes:
    """The factory-erase UF2, as `tools/make_factory_erase_uf2.py` writes it: one block,
    family 0x4D455348, address 0, block 0 of 1, an all-zero 256-byte payload."""
    header = struct.pack(
        "<8I",
        UF2_MAGIC_START0,
        UF2_MAGIC_START1,
        UF2_FLAG_FAMILYID,
        0,
        UF2_PAYLOAD_SIZE,
        0,
        1,
        FAMILY_FACTORY_ERASE,
    )
    return header + bytes(476) + struct.pack("<I", UF2_MAGIC_END)


def scan_uf2(data: bytes) -> dict[str, Any]:
    """Validate UF2 bytes and summarize them. Fails on the first block whose magic is wrong."""
    if not data:
        return {"ok": False, "error": "empty file, not a UF2"}
    if len(data) % UF2_BLOCK_SIZE:
        return {
            "ok": False,
            "error": f"size {len(data)} is not a multiple of {UF2_BLOCK_SIZE}; not a UF2",
        }
    families: dict[int, int] = {}
    declared: set[int] = set()
    lo: int | None = None
    hi: int | None = None
    noflash = 0
    unacceptable = 0
    payload_total = 0
    for offset in range(0, len(data), UF2_BLOCK_SIZE):
        s0, s1, flags, addr, psize, _bno, nblocks, fam = struct.unpack_from("<8I", data, offset)
        (end,) = struct.unpack_from("<I", data, offset + UF2_BLOCK_SIZE - 4)
        if (s0, s1, end) != (UF2_MAGIC_START0, UF2_MAGIC_START1, UF2_MAGIC_END):
            return {
                "ok": False,
                "error": (
                    f"block {offset // UF2_BLOCK_SIZE} (offset {offset}) has bad UF2 magic "
                    f"start0={_hex(s0)} start1={_hex(s1)} end={_hex(end)}; expected "
                    f"{_hex(UF2_MAGIC_START0)}/{_hex(UF2_MAGIC_START1)}/{_hex(UF2_MAGIC_END)}"
                ),
            }
        declared.add(nblocks)
        if flags & UF2_FLAG_NOFLASH:
            noflash += 1
            continue
        # ghostfat.c is_uf2_block(): what the bootloader will consider at all.
        if not (flags & UF2_FLAG_FAMILYID) or psize != UF2_PAYLOAD_SIZE or addr & 0xFF:
            unacceptable += 1
            continue
        families[fam] = families.get(fam, 0) + 1
        payload_total += psize
        lo = addr if lo is None else min(lo, addr)
        hi = addr + psize if hi is None else max(hi, addr + psize)

    block_count = len(data) // UF2_BLOCK_SIZE
    fam_list = [dict(family_info(f), blocks=n) for f, n in sorted(families.items())]
    single = next(iter(families)) if len(families) == 1 else None
    return {
        "ok": True,
        "block_count": block_count,
        "declared_num_blocks": sorted(declared)[0] if len(declared) == 1 else sorted(declared),
        "families": fam_list,
        "family_id": _hex(single) if single is not None else None,
        "kind": family_info(single)["kind"]
        if single is not None
        else ("mixed" if families else None),
        "target_start": _hex(lo) if lo is not None else None,
        "target_end": _hex(hi) if hi is not None else None,
        "payload_bytes": payload_total,
        "noflash_blocks": noflash,
        "blocks_bootloader_ignores": unacceptable,
        "is_factory_erase": set(families) == {FAMILY_FACTORY_ERASE},
        "is_bootloader_update": FAMILY_BOOTLOADER in families,
    }


def uf2_file_info(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Family id(s), target address range and block count of a `.uf2` file.

    `target_end` is exclusive. `blocks_bootloader_ignores` counts blocks the bootloader's
    `is_uf2_block()` refuses (no family-id flag, payload not 256 bytes, unaligned address).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "path": str(p), "error": f"not a file: {p}"}
    size = p.stat().st_size
    if size > _MAX_UF2_BYTES:
        return {"ok": False, "path": str(p), "size": size, "error": "too large to be a UF2"}
    result = scan_uf2(p.read_bytes())
    return {"path": str(p), "size": size, **result}


# --------------------------------------------------------------------------- INFO_UF2.TXT

_SD_RE = re.compile(r"\bS(\d{3})\s+(?:version\s+)?(\d+\.\d+\.\d+)", re.IGNORECASE)
_FAULT_RE = re.compile(
    r"id=0x([0-9a-fA-F]+)\s+pc=0x([0-9a-fA-F]+)\s+info=0x([0-9a-fA-F]+)", re.IGNORECASE
)
_FAMILY_RE = re.compile(r"0x([0-9a-fA-F]{1,8})")


def parse_info_uf2(text: str) -> dict[str, Any]:
    """Parse `INFO_UF2.TXT` (ghostfat.c `INFO_UF2_TEXT` plus what `uf2_init()` appends).

    Lines, in order: `UF2 Bootloader <version> <submodule versions>`, `Model:`, `Board-ID:`,
    `Date:`, `Factory-Erase: UF2 family 0x4D455348` (OTAFIX 2.4+), `SoftDevice: S140 6.1.1`
    or `SoftDevice: not found`, `Last-Fault: id=0x.. pc=0x.. info=0x..` (2.6.0+, only when
    the bootloader recorded one). Every line but the first is optional; older Adafruit
    builds put the SoftDevice at the end of the version line instead (`... s140 6.1.1`).
    """
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    fields: dict[str, str] = {}
    bootloader_line: str | None = None
    for ln in lines:
        if ln.startswith("UF2 Bootloader") and bootloader_line is None:
            bootloader_line = ln
            continue
        key, sep, value = ln.partition(":")
        if sep and key and " " not in key.strip():
            fields.setdefault(key.strip(), value.strip())

    version: str | None = None
    version_detail: str | None = None
    if bootloader_line is not None:
        rest = bootloader_line[len("UF2 Bootloader") :].strip()
        version, _, version_detail = rest.partition(" ")
        version = version or None

    softdevice: str | None = None
    softdevice_present: bool | None = None
    sd_raw = fields.get("SoftDevice")
    sd_source = sd_raw
    if sd_raw is None and bootloader_line is not None and _SD_RE.search(bootloader_line):
        sd_source = bootloader_line
    if sd_source is not None:
        m = _SD_RE.search(sd_source)
        if m:
            softdevice = f"S{m.group(1)} {m.group(2)}"
            softdevice_present = True
        elif sd_raw is not None and "not found" in sd_raw.lower():
            softdevice_present = False

    fe_raw = next((ln for ln in lines if ln.startswith("Factory-Erase:")), None)
    fe_family: str | None = None
    if fe_raw is not None:
        fm = _FAMILY_RE.search(fe_raw)
        if fm:
            fe_family = _hex(int(fm.group(1), 16))

    lf_raw = next((ln for ln in lines if ln.startswith("Last-Fault:")), None)
    last_fault: dict[str, Any] | None = None
    if lf_raw is not None:
        last_fault = {"raw": lf_raw}
        lm = _FAULT_RE.search(lf_raw)
        if lm:
            last_fault.update(
                id=_hex(int(lm.group(1), 16)),
                pc=_hex(int(lm.group(2), 16)),
                info=_hex(int(lm.group(3), 16)),
            )

    return {
        "is_uf2_bootloader": bootloader_line is not None,
        "bootloader_version": version,
        "bootloader_version_detail": version_detail or None,
        "model": fields.get("Model"),
        "board_id": fields.get("Board-ID"),
        "date": fields.get("Date"),
        "softdevice": softdevice,
        "softdevice_present": softdevice_present,
        "softdevice_raw": sd_raw,
        "factory_erase": {
            "supported": fe_raw is not None,
            "family_id": fe_family,
            "raw": fe_raw,
        },
        "last_fault": last_fault,
        "fields": fields,
        "raw": text,
    }


# --------------------------------------------------------------------------- drive discovery


def _find_entry(directory: Path, name: str) -> Path | None:
    """Case-insensitive lookup: vfat's `shortname=` mount option can lower-case 8.3 names."""
    direct = directory / name
    try:
        if direct.is_file():
            return direct
        for child in directory.iterdir():
            if child.name.upper() == name and child.is_file():
                return child
    except OSError:
        return None
    return None


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_proc_mounts(text: str) -> list[dict[str, str]]:
    """FAT mounts from `/proc/mounts` text (octal escapes such as `\\040` decoded)."""
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in _FAT_FSTYPES:
            out.append(
                {
                    "device": _unescape_mount(parts[0]),
                    "mount": _unescape_mount(parts[1]),
                    "fstype": parts[2],
                }
            )
    return out


def default_roots(platform: str | None = None) -> list[Path]:
    """Directories whose children may be mounted UF2 drives on this OS."""
    plat = platform or sys.platform
    if plat == "darwin":
        return [Path("/Volumes")]
    if plat.startswith("win"):
        return [Path(f"{letter}:\\") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    roots = [Path("/run/media") / user, Path("/media") / user] if user else []
    return [*roots, Path("/media"), Path("/mnt")]


def _label_for_device(device: str, by_label_dir: Path) -> str | None:
    try:
        target = os.path.realpath(device)
        for link in by_label_dir.iterdir():
            if os.path.realpath(link) == target:
                return link.name.replace("\\x20", " ")
    except OSError:
        return None
    return None


def _describe_drive(mount: Path, device: str | None, by_label_dir: Path) -> dict[str, Any] | None:
    info_path = _find_entry(mount, INFO_FILE)
    if info_path is None:
        return None
    try:
        text = info_path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    current = _find_entry(mount, CURRENT_FILE)
    current_size: int | None = None
    if current is not None:
        try:
            current_size = current.stat().st_size
        except OSError:
            current = None
    label = (_label_for_device(device, by_label_dir) if device else None) or mount.name
    return {
        "mount": str(mount),
        "label": label,
        "device": device,
        "info_file": str(info_path),
        "current_uf2": str(current) if current is not None else None,
        "current_uf2_size": current_size,
        "info": parse_info_uf2(text),
    }


def find_uf2_drives(
    roots: Sequence[str | os.PathLike[str]] | None = None,
    mounts: str | None = None,
    by_label_dir: str | os.PathLike[str] = "/dev/disk/by-label",
    platform: str | None = None,
) -> dict[str, Any]:
    """Mounted bootloader UF2 drives: any mount whose root holds `INFO_UF2.TXT`.

    Candidates are FAT mounts from `mounts` (`/proc/mounts` text; read from the system on
    Linux when None) plus each root and its immediate children (`default_roots()` when None).
    The label is the `/dev/disk/by-label` name of the mount's device when known, otherwise
    the mount directory's name (udisks and macOS name the mount after the volume label).
    """
    plat = platform or sys.platform
    if mounts is None and plat.startswith("linux"):
        try:
            mounts = Path("/proc/mounts").read_text()
        except OSError:
            mounts = ""
    root_paths = [Path(r) for r in roots] if roots is not None else default_roots(plat)

    candidates: dict[str, tuple[Path, str | None]] = {}
    for m in parse_proc_mounts(mounts or ""):
        mp = Path(m["mount"])
        candidates.setdefault(os.path.realpath(mp), (mp, m["device"]))
    for root in root_paths:
        try:
            if not root.is_dir():
                continue
            children = [root, *sorted(c for c in root.iterdir() if c.is_dir())]
        except OSError:
            continue
        for child in children:
            candidates.setdefault(os.path.realpath(child), (child, None))

    drives = []
    for mp, device in candidates.values():
        d = _describe_drive(mp, device, Path(by_label_dir))
        if d is not None:
            drives.append(d)
    return {
        "ok": True,
        "count": len(drives),
        "drives": drives,
        "searched_roots": [str(r) for r in root_paths],
        "fat_mounts_checked": len(parse_proc_mounts(mounts or "")),
    }


# --------------------------------------------------------------------------- drive <-> board


def _sysfs_usb_serial(device: str, sys_root: Path) -> str | None:
    """Walk up from `/sys/class/block/<dev>` to the USB device dir (has `idVendor`) and read
    its `serial`."""
    name = Path(os.path.realpath(device)).name
    node = sys_root / "class" / "block" / name
    try:
        if not node.exists():
            return None
        cur = node.resolve()
        stop = sys_root.resolve()
    except OSError:
        return None
    for parent in [cur, *cur.parents]:
        if parent == stop or parent == parent.parent:
            break
        serial = parent / "serial"
        if serial.is_file() and (parent / "idVendor").is_file():
            try:
                return serial.read_text().strip() or None
            except OSError:
                return None
    return None


_BY_ID_RE = re.compile(r"^usb-.*_([^_]+)-\d+:\d+(?:-part\d+)?$")


def _by_id_usb_serial(device: str, by_id_dir: Path) -> str | None:
    """Serial from the `/dev/disk/by-id/usb-<vendor>_<model>_<serial>-0:0` link that points
    at `device` (the bootloader reports vendor `Mesh`, model `Meshtastic UF2`)."""
    target = os.path.realpath(device)
    parent = re.sub(r"(?<=[a-z])\d+$", "", target)
    try:
        links = sorted(by_id_dir.iterdir())
    except OSError:
        return None
    for link in links:
        m = _BY_ID_RE.match(link.name)
        if m and os.path.realpath(link) in (target, parent):
            return m.group(1)
    return None


def drive_usb_serial(
    drive: Mapping[str, Any],
    by_id_dir: str | os.PathLike[str] = "/dev/disk/by-id",
    sys_root: str | os.PathLike[str] = "/sys",
) -> dict[str, Any]:
    """USB serial number of a drive's block device, Linux only (sysfs, then by-id)."""
    device = drive.get("device")
    if not device:
        return {"serial": None, "method": None, "reason": "no block device known for this drive"}
    s = _sysfs_usb_serial(str(device), Path(sys_root))
    if s:
        return {"serial": s, "method": "sysfs"}
    s = _by_id_usb_serial(str(device), Path(by_id_dir))
    if s:
        return {"serial": s, "method": "by-id"}
    return {"serial": None, "method": None, "reason": f"no USB serial found for {device}"}


def port_usb_serial(port: str, ports: Iterable[Any] | None = None) -> str | None:
    """USB serial number of a serial port from pyserial's port listing (no port is opened).

    The Adafruit core the firmware runs on and the bootloader both derive the USB serial
    from FICR DEVICEID as 16 upper-case hex digits, so the application's CDC port and the
    bootloader's drive should report the same string. Read it before entering DFU: the
    port path can change across the reset.
    """
    if ports is None:
        from serial.tools import list_ports

        ports = list_ports.comports()
    want = os.path.realpath(port)
    for p in ports:
        dev = p.get("device") if isinstance(p, Mapping) else getattr(p, "device", None)
        sn = p.get("serial_number") if isinstance(p, Mapping) else getattr(p, "serial_number", None)
        if dev and (dev == port or os.path.realpath(dev) == want):
            return str(sn) if sn else None
    return None


def find_drive_for_port(
    port: str | None = None,
    serial_number: str | None = None,
    drives: Sequence[Mapping[str, Any]] | None = None,
    ports: Iterable[Any] | None = None,
    by_id_dir: str | os.PathLike[str] = "/dev/disk/by-id",
    sys_root: str | os.PathLike[str] = "/sys",
) -> dict[str, Any]:
    """Pick the UF2 drive that belongs to one board.

    Best effort: matches the board's USB serial (given, or looked up from `port`) against
    each drive's USB serial (Linux sysfs or `/dev/disk/by-id`). When no drive's serial can
    be read (macOS, Windows, a mount not in /proc/mounts) and exactly one UF2 drive is
    mounted, that drive is returned with `verified: False`. A single drive whose serial is
    known and differs is refused.
    """
    drive_list = list(drives) if drives is not None else find_uf2_drives()["drives"]
    serial = serial_number or (port_usb_serial(port, ports) if port else None)
    seen = []
    for d in drive_list:
        ds = drive_usb_serial(d, by_id_dir, sys_root)
        seen.append({"mount": d.get("mount"), "serial": ds["serial"], "method": ds["method"]})
        if serial and ds["serial"] and ds["serial"].upper() == serial.upper():
            return {
                "ok": True,
                "drive": dict(d),
                "method": f"usb_serial_{ds['method']}",
                "verified": True,
                "serial": serial,
                "candidates": seen,
            }

    base = {"serial": serial, "candidates": seen}
    if not drive_list:
        return {"ok": False, "drive": None, "error": "no UF2 drive mounted", **base}
    known = [c for c in seen if c["serial"]]
    if len(drive_list) == 1 and not (serial and known):
        return {
            "ok": True,
            "drive": dict(drive_list[0]),
            "method": "only_drive",
            "verified": False,
            "note": "the only UF2 drive mounted; not tied to the board by USB serial",
            **base,
        }
    if serial:
        return {
            "ok": False,
            "drive": None,
            "error": f"no UF2 drive reports USB serial {serial}",
            **base,
        }
    return {
        "ok": False,
        "drive": None,
        "error": f"{len(drive_list)} UF2 drives mounted; pass the board's port or serial number",
        **base,
    }


# --------------------------------------------------------------------------- drive operations


def _drive_mount(drive: DrivePath) -> Path:
    if isinstance(drive, Mapping):
        return Path(str(drive["mount"]))
    return Path(drive)


def _is_present(mount: Path) -> bool:
    try:
        return _find_entry(mount, INFO_FILE) is not None
    except OSError:
        return False


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "nrf52"


def backup_current_uf2(
    drive: DrivePath,
    dest_dir: str | os.PathLike[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy `CURRENT.UF2` (a dump of the installed application) off the drive to
    `<dest_dir>/<label>-<board_id>-CURRENT-<YYYYmmdd-HHMMSS>.uf2`. Copy it back with
    `flash_uf2` to restore."""
    mount = _drive_mount(drive)
    src = _find_entry(mount, CURRENT_FILE)
    if src is None:
        return {"ok": False, "drive": str(mount), "error": f"no {CURRENT_FILE} on {mount}"}
    info_path = _find_entry(mount, INFO_FILE)
    board = ""
    if info_path is not None:
        board = (
            parse_info_uf2(info_path.read_text(encoding="ascii", errors="replace"))["board_id"]
            or ""
        )
    label = drive.get("label") if isinstance(drive, Mapping) else None
    stem = _safe_name("-".join(x for x in (str(label or mount.name), board) if x))
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{stem}-CURRENT-{stamp}.uf2"
    n = 1
    while out.exists():
        out = dest / f"{stem}-CURRENT-{stamp}-{n}.uf2"
        n += 1
    try:
        shutil.copyfile(src, out)
    except OSError as exc:
        return {"ok": False, "drive": str(mount), "error": f"copy failed: {exc}"}
    summary = uf2_file_info(out)
    return {
        "ok": bool(summary.get("ok")),
        "drive": str(mount),
        "source": str(src),
        "path": str(out),
        "size": out.stat().st_size,
        "uf2": summary,
    }


def _copy_to_drive(src: Path, dest: Path) -> tuple[int, str | None]:
    written = 0
    try:
        with src.open("rb") as fin, dest.open("wb") as fout:
            while chunk := fin.read(64 * 1024):
                fout.write(chunk)
                written += len(chunk)
            fout.flush()
            os.fsync(fout.fileno())
    except OSError as exc:
        return written, str(exc)
    return written, None


def _wait_gone(
    mount: Path,
    timeout_s: float,
    poll_s: float,
    present: Callable[[Path], bool],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[bool, float]:
    start = clock()
    while True:
        if not present(mount):
            return True, clock() - start
        if clock() - start >= timeout_s:
            return False, clock() - start
        sleep(poll_s)


def _write_and_wait(
    mount: Path,
    src: Path,
    size: int,
    timeout_s: float,
    poll_s: float,
    present: Callable[[Path], bool] | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, Any]:
    dest = mount / src.name
    written, err = _copy_to_drive(src, dest)
    if err is not None and written < size:
        return {
            "ok": False,
            "outcome": "write_error",
            "bytes_written": written,
            "error": f"write to {dest} failed after {written} of {size} bytes: {err}",
        }
    detached, elapsed = _wait_gone(mount, timeout_s, poll_s, present or _is_present, sleep, clock)
    result: dict[str, Any] = {
        "ok": detached,
        "outcome": "detached" if detached else "still_present",
        "written_to": str(dest),
        "bytes_written": written,
        "wait_s": round(elapsed, 3),
    }
    if err is not None:
        # The bootloader resets as soon as the last block lands; the close can lose the race.
        result["close_error"] = err
    return result


def _check_bootloader_drive(mount: Path) -> str | None:
    if not mount.is_dir():
        return f"{mount} is not a mounted directory"
    if _find_entry(mount, INFO_FILE) is None:
        return f"{mount} has no {INFO_FILE}; not a UF2 bootloader drive"
    return None


def flash_uf2(
    drive: DrivePath,
    uf2_path: str | os.PathLike[str],
    timeout_s: float = 30.0,
    poll_s: float = 0.25,
    present: Callable[[Path], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Copy one `.uf2` onto a bootloader drive, fsync, then wait for the drive to go away.

    Refuses a file that fails the UF2 magic check, a file with no block the bootloader
    accepts, and the factory-erase UF2 (use `factory_erase`). `outcome` is `detached` (the
    board took the file and reset), `still_present` (the drive stayed past `timeout_s`; a
    rejected file - wrong SoftDevice, unknown family, bootloader update for another USB
    id - leaves it up) or `write_error`. A full firmware copy can take tens of seconds, so
    an MCP tool should run this as a job.
    """
    mount = _drive_mount(drive)
    src = Path(uf2_path).expanduser()
    base = {"drive": str(mount), "file": str(src)}
    problem = _check_bootloader_drive(mount)
    if problem:
        return {"ok": False, "error": problem, **base}
    info = uf2_file_info(src)
    if not info.get("ok"):
        return {"ok": False, "error": f"refusing: {info.get('error')}", "uf2": info, **base}
    if info["is_factory_erase"]:
        return {
            "ok": False,
            "error": "refusing: this is the factory-erase UF2; use factory_erase()",
            "uf2": info,
            **base,
        }
    if not info["families"]:
        return {
            "ok": False,
            "error": (
                "refusing: no block the bootloader accepts (needs the family-id flag, a "
                "256-byte payload and a 256-byte aligned address)"
            ),
            "uf2": info,
            **base,
        }
    try:
        if src.resolve().is_relative_to(mount.resolve()):
            return {"ok": False, "error": "refusing: source file is on the drive itself", **base}
    except OSError:
        pass
    result = _write_and_wait(mount, src, info["size"], timeout_s, poll_s, present, sleep, clock)
    if result["outcome"] == "still_present":
        result["error"] = (
            f"drive still mounted after {timeout_s}s; the bootloader may have rejected the "
            "file (WRONG FILE on boards with a screen)"
        )
    return {**base, "uf2": info, **result}


def find_factory_erase_uf2(
    bootloader_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Locate `tools/meshtastic_factory_erase.uf2` in an nRF52_Bootloader checkout, if given.

    The file is also attached to every bootloader release; `factory_erase_uf2_bytes()`
    produces the same 512 bytes.
    """
    if bootloader_root is None:
        return {"ok": False, "path": None, "error": "no nRF52_Bootloader checkout given"}
    p = Path(bootloader_root).expanduser() / FACTORY_ERASE_UF2_RELPATH
    if not p.is_file():
        return {"ok": False, "path": None, "error": f"not found: {p}"}
    info = uf2_file_info(p)
    if not (info.get("ok") and info["is_factory_erase"]):
        return {
            "ok": False,
            "path": str(p),
            "error": "file is not the factory-erase UF2",
            "uf2": info,
        }
    return {"ok": True, "path": str(p), "uf2": info}


def factory_erase(
    drive: DrivePath,
    erase_uf2_path: str | os.PathLike[str],
    timeout_s: float = 10.0,
    poll_s: float = 0.25,
    present: Callable[[Path], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Copy the factory-erase UF2 onto the drive: the bootloader erases the application's
    data region (config, node DB, keys, BLE bonds), leaves the application in place, and
    resets back into UF2 mode.

    Refuses unless `INFO_UF2.TXT` carries a `Factory-Erase:` line (older bootloaders ignore
    the family) and the file is exactly the erase family. The drive detaches about half a
    second after the copy and returns a second or two later, possibly at a new mount path;
    call `find_uf2_drives` again.
    """
    mount = _drive_mount(drive)
    src = Path(erase_uf2_path).expanduser()
    base = {"drive": str(mount), "file": str(src)}
    problem = _check_bootloader_drive(mount)
    if problem:
        return {"ok": False, "error": problem, **base}
    info_path = _find_entry(mount, INFO_FILE)
    assert info_path is not None
    parsed = parse_info_uf2(info_path.read_text(encoding="ascii", errors="replace"))
    fe = parsed["factory_erase"]
    if not fe["supported"]:
        return {
            "ok": False,
            "error": (
                f"refusing: {INFO_FILE} has no Factory-Erase line; this bootloader "
                f"({parsed['bootloader_version']}) does not accept the erase UF2"
            ),
            **base,
        }
    if fe["family_id"] not in (None, _hex(FAMILY_FACTORY_ERASE)):
        return {
            "ok": False,
            "error": f"refusing: bootloader advertises erase family {fe['family_id']}",
            **base,
        }
    info = uf2_file_info(src)
    if not info.get("ok"):
        return {"ok": False, "error": f"refusing: {info.get('error')}", "uf2": info, **base}
    if not info["is_factory_erase"]:
        return {
            "ok": False,
            "error": (
                f"refusing: {src.name} is not the factory-erase UF2 (family {info['family_id']})"
            ),
            "uf2": info,
            **base,
        }
    result = _write_and_wait(mount, src, info["size"], timeout_s, poll_s, present, sleep, clock)
    if result["outcome"] == "detached":
        result["note"] = "erase sent; the drive returns in a second or two"
    else:
        result.setdefault("error", f"drive still mounted after {timeout_s}s")
    return {**base, "bootloader_version": parsed["bootloader_version"], "uf2": info, **result}
