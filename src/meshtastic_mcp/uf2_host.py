# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Mount and eject an nRF52 bootloader's UF2 drive from the host.

Linux goes through udisks2 (`udisksctl`, and `gdbus` for `Drive.Eject`, the SCSI eject the
bootloader sees), which polkit allows only from a desktop session unless a rule grants it;
macOS uses `diskutil`. A polkit refusal raises `NotAuthorized` naming the actions to grant.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .nrf52_bootloader import _BY_ID_RE

# The udisks2 actions each operation needs from a session that is not the local desktop seat
# (SSH, a systemd user service). A desktop session and macOS need none of this.
MOUNT_ACTIONS = (
    "org.freedesktop.udisks2.filesystem-mount",
    "org.freedesktop.udisks2.filesystem-mount-other-seat",
)
EJECT_ACTIONS = (
    "org.freedesktop.udisks2.filesystem-unmount-others",
    "org.freedesktop.udisks2.eject-media",
    "org.freedesktop.udisks2.eject-media-other-seat",
)
POLKIT_ACTIONS = MOUNT_ACTIONS + EJECT_ACTIONS


def polkit_rule_command(actions: Sequence[str], user: str | None = None) -> str:
    """A one-shot command that grants `actions` to `user`; polkit reads it without a restart."""
    who = user or os.environ.get("USER") or os.environ.get("LOGNAME") or "<user>"
    ids = " ||\n       ".join(f'action.id == "{a}"' for a in actions)
    return (
        "sudo tee /etc/polkit-1/rules.d/50-meshtastic-uf2.rules >/dev/null <<'EOF'\n"
        "polkit.addRule(function(action, subject) {\n"
        f'  if (subject.user == "{who}" &&\n      ({ids})) {{\n'
        "    return polkit.Result.YES;\n  }\n});\nEOF"
    )


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class HostDriveError(RuntimeError):
    """The host could not mount or eject the drive."""


class NotAuthorized(HostDriveError):
    """polkit refused a udisks2 action."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)


def _checked(argv: Sequence[str], run: Runner, actions: Sequence[str] = POLKIT_ACTIONS) -> str:
    if shutil.which(argv[0]) is None and run is _run:
        raise HostDriveError(f"{argv[0]} is not installed on this host")
    proc = run(argv)
    out = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    if proc.returncode != 0:
        if "NotAuthorized" in out or "not authorized" in out.lower():
            raise NotAuthorized(
                f"polkit refused `{' '.join(argv)}` because this is not the desktop session "
                f"({out}). Run this once on the host to allow it:\n"
                f"{polkit_rule_command(actions)}"
            )
        raise HostDriveError(f"`{' '.join(argv)}` failed: {out}")
    return out


def block_device_for_serial(
    serial: str, by_id_dir: str | os.PathLike[str] = "/dev/disk/by-id"
) -> str | None:
    """The block device whose `/dev/disk/by-id/usb-*_<serial>-0:0` link names this USB serial.

    Matches any vendor and model, so both `Adafruit_nRF_UF2_<serial>` and the 2.6.1+
    bootloader's `Mesh_Meshtastic_UF2_<serial>` resolve.
    """
    try:
        links = sorted(Path(by_id_dir).iterdir())
    except OSError:
        return None
    for link in links:
        m = _BY_ID_RE.match(link.name)
        if m and m.group(1).upper() == serial.upper() and "-part" not in link.name:
            return os.path.realpath(link)
    return None


def _drive_object(device: str, run: Runner) -> str:
    out = _checked(["udisksctl", "info", "-b", device], run)
    m = re.search(r"Drive:\s+'(/org/freedesktop/UDisks2/drives/[^']+)'", out)
    if not m:
        raise HostDriveError(f"udisks2 names no drive for {device}")
    return m.group(1)


def mount(device: str, platform: str | None = None, run: Runner = _run) -> str:
    """Mount `device` and return what the tool printed."""
    plat = platform or sys.platform
    if plat == "darwin":
        return _checked(["diskutil", "mount", device], run)
    return _checked(
        ["udisksctl", "mount", "--no-user-interaction", "-b", device], run, MOUNT_ACTIONS
    )


def eject(
    device: str | None,
    mount_point: str | None,
    platform: str | None = None,
    run: Runner = _run,
) -> list[str]:
    """Unmount the drive if mounted, then eject it. Returns each step's output."""
    plat = platform or sys.platform
    if plat == "darwin":
        target = mount_point or device
        if not target:
            raise HostDriveError("no mount point or device to eject")
        return [_checked(["diskutil", "eject", target], run)]
    if not device:
        raise HostDriveError("no block device known for this drive, so udisks2 cannot eject it")
    steps = []
    if mount_point and os.path.ismount(mount_point):
        steps.append(
            _checked(
                ["udisksctl", "unmount", "--no-user-interaction", "-b", device], run, EJECT_ACTIONS
            )
        )
    drive = _drive_object(device, run)
    steps.append(
        _checked(
            [
                "gdbus",
                "call",
                "--system",
                "--dest",
                "org.freedesktop.UDisks2",
                "--object-path",
                drive,
                "--method",
                "org.freedesktop.UDisks2.Drive.Eject",
                "{}",
            ],
            run,
            EJECT_ACTIONS,
        )
    )
    return steps
