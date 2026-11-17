# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""nrf52_bootloader against fake UF2 drives in tmp_path - no hardware, no real mounts."""

from __future__ import annotations

import os
import struct
from datetime import datetime
from pathlib import Path

import pytest

from meshtastic_mcp import nrf52_bootloader as nb

INFO_261 = (
    "UF2 Bootloader 2.6.1 lib/nrfx (v2.0.0) lib/tinyusb (0.12.0-145-g9775e7691)\r\n"
    "Model: WisBlock RAK4631 Board\r\n"
    "Board-ID: WisBlock-RAK4631-Board\r\n"
    "Date: Nov 10 2026\r\n"
    "Factory-Erase: UF2 family 0x4D455348\r\n"
    "SoftDevice: S140 6.1.1\r\n"
    "Last-Fault: id=0x4001 pc=0x0 info=0xd\r\n"
)
INFO_OTAFIX1 = (
    "UF2 Bootloader 0.9.2-otafix1 lib/nrfx (v2.0.0)\r\n"
    "Model: Seeed T1000-E\r\n"
    "Board-ID: T1000-E-v1\r\n"
    "Date: Jan  2 2024\r\n"
    "SoftDevice: S140 7.3.0\r\n"
)
INFO_ADAFRUIT_029 = (
    "UF2 Bootloader 0.2.9 lib/nrfx (v1.1.0-1-g096e770) lib/tinyusb (legacy-525-ga1c59649) "
    "s140 6.1.1\r\n"
    "Model: Adafruit Feather nRF52840 Express\r\n"
    "Board-ID: nRF52840-Feather-revD\r\n"
    "Date: Dec 31 2018\r\n"
)


def uf2_block(
    addr: int,
    block_no: int,
    num_blocks: int,
    family: int = nb.FAMILY_NRF52840_APP,
    flags: int = nb.UF2_FLAG_FAMILYID,
    payload: int = 256,
    end: int = nb.UF2_MAGIC_END,
) -> bytes:
    header = struct.pack(
        "<8I",
        nb.UF2_MAGIC_START0,
        nb.UF2_MAGIC_START1,
        flags,
        addr,
        payload,
        block_no,
        num_blocks,
        family,
    )
    return header + bytes(476) + struct.pack("<I", end)


def app_image(blocks: int = 4, start: int = 0x26000, family: int = nb.FAMILY_NRF52840_APP) -> bytes:
    return b"".join(uf2_block(start + i * 256, i, blocks, family) for i in range(blocks))


def make_drive(
    root: Path, label: str = "RAK4631", info: str = INFO_261, current: bool = True
) -> Path:
    d = root / label
    d.mkdir(parents=True)
    (d / "INFO_UF2.TXT").write_bytes(info.encode())
    (d / "INDEX.HTM").write_text("<html></html>")
    if current:
        (d / "CURRENT.UF2").write_bytes(app_image(8))
    return d


class Vanishing:
    """`present` stand-in: the drive is there for `polls` checks, then gone."""

    def __init__(self, polls: int) -> None:
        self.polls = polls
        self.calls = 0

    def __call__(self, _mount: Path) -> bool:
        self.calls += 1
        return self.calls <= self.polls


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


# --------------------------------------------------------------------------- parse_info_uf2


def test_parse_info_current_format():
    p = nb.parse_info_uf2(INFO_261)
    assert p["is_uf2_bootloader"] is True
    assert p["bootloader_version"] == "2.6.1"
    assert p["bootloader_version_detail"].startswith("lib/nrfx")
    assert p["model"] == "WisBlock RAK4631 Board"
    assert p["board_id"] == "WisBlock-RAK4631-Board"
    assert p["date"] == "Nov 10 2026"
    assert p["softdevice"] == "S140 6.1.1"
    assert p["softdevice_present"] is True
    assert p["factory_erase"] == {
        "supported": True,
        "family_id": "0x4D455348",
        "raw": "Factory-Erase: UF2 family 0x4D455348",
    }
    assert p["last_fault"] == {
        "raw": "Last-Fault: id=0x4001 pc=0x0 info=0xd",
        "id": "0x00004001",
        "pc": "0x00000000",
        "info": "0x0000000D",
    }
    assert p["raw"] == INFO_261


def test_parse_info_pre_factory_erase_bootloader():
    p = nb.parse_info_uf2(INFO_OTAFIX1)
    assert p["bootloader_version"] == "0.9.2-otafix1"
    assert p["softdevice"] == "S140 7.3.0"
    assert p["factory_erase"]["supported"] is False
    assert p["factory_erase"]["raw"] is None
    assert p["last_fault"] is None


def test_parse_info_softdevice_on_version_line():
    p = nb.parse_info_uf2(INFO_ADAFRUIT_029)
    assert p["bootloader_version"] == "0.2.9"
    assert p["softdevice"] == "S140 6.1.1"
    assert p["softdevice_raw"] is None
    assert p["board_id"] == "nRF52840-Feather-revD"


def test_parse_info_softdevice_not_found_and_lf_only():
    text = INFO_261.replace("SoftDevice: S140 6.1.1", "SoftDevice: not found").replace("\r\n", "\n")
    p = nb.parse_info_uf2(text)
    assert p["softdevice"] is None
    assert p["softdevice_present"] is False
    assert p["model"] == "WisBlock RAK4631 Board"


def test_parse_info_garbage():
    p = nb.parse_info_uf2("hello\nworld")
    assert p["is_uf2_bootloader"] is False
    assert p["bootloader_version"] is None
    assert p["factory_erase"]["supported"] is False


# --------------------------------------------------------------------------- UF2 file info


def test_uf2_info_application(tmp_path):
    f = tmp_path / "firmware.uf2"
    f.write_bytes(app_image(4))
    info = nb.uf2_file_info(f)
    assert info["ok"] is True
    assert info["block_count"] == 4
    assert info["declared_num_blocks"] == 4
    assert info["family_id"] == "0xADA52840"
    assert info["kind"] == "application"
    assert info["target_start"] == "0x00026000"
    assert info["target_end"] == "0x00026400"
    assert info["payload_bytes"] == 1024
    assert info["is_factory_erase"] is False


def test_uf2_info_factory_erase_matches_generator_layout(tmp_path):
    data = nb.factory_erase_uf2_bytes()
    assert len(data) == 512
    f = tmp_path / "meshtastic_factory_erase.uf2"
    f.write_bytes(data)
    info = nb.uf2_file_info(f)
    assert info["is_factory_erase"] is True
    assert info["kind"] == "factory_erase"
    assert info["block_count"] == 1
    # Same bytes the bootloader repo's generator emits for (start0,start1,flags,addr,...).
    assert data[:32] == struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, 0, 256, 0, 1, 0x4D455348)


def test_uf2_info_board_family_and_bootloader_update(tmp_path):
    f = tmp_path / "update.uf2"
    f.write_bytes(
        uf2_block(0xF4000, 0, 2, nb.FAMILY_BOOTLOADER)
        + uf2_block(0x10001000, 1, 2, nb.FAMILY_BOOTLOADER)
    )
    assert nb.uf2_file_info(f)["is_bootloader_update"] is True
    g = tmp_path / "board.uf2"
    g.write_bytes(app_image(2, family=0x239A0029))
    info = nb.uf2_file_info(g)
    assert info["kind"] == "board_application"
    assert "239A:0029" in info["families"][0]["name"]


def test_uf2_info_rejects_non_uf2(tmp_path):
    f = tmp_path / "x.uf2"
    f.write_bytes(b"\x00" * 1024)
    info = nb.uf2_file_info(f)
    assert info["ok"] is False
    assert "bad UF2 magic" in info["error"]
    g = tmp_path / "short.uf2"
    g.write_bytes(app_image(1)[:300])
    assert "not a multiple of 512" in nb.uf2_file_info(g)["error"]
    bad_end = tmp_path / "end.uf2"
    bad_end.write_bytes(app_image(1) + uf2_block(0x26100, 1, 2, end=0))
    assert "block 1" in nb.uf2_file_info(bad_end)["error"]
    assert nb.uf2_file_info(tmp_path / "missing.uf2")["ok"] is False


def test_uf2_info_counts_blocks_bootloader_ignores(tmp_path):
    f = tmp_path / "odd.uf2"
    f.write_bytes(
        uf2_block(0x26000, 0, 3)
        + uf2_block(0x26100, 1, 3, flags=0)  # no family-id flag
        + uf2_block(0x26201, 2, 3)  # unaligned
    )
    info = nb.uf2_file_info(f)
    assert info["block_count"] == 3
    assert info["blocks_bootloader_ignores"] == 2
    assert info["families"][0]["blocks"] == 1


# --------------------------------------------------------------------------- discovery


def test_find_uf2_drives_in_roots(tmp_path):
    media = tmp_path / "media"
    make_drive(media, "RAK4631")
    (media / "USBSTICK").mkdir()
    (media / "USBSTICK" / "readme.txt").write_text("not a bootloader")
    res = nb.find_uf2_drives(roots=[media], mounts="", platform="linux")
    assert res["count"] == 1
    d = res["drives"][0]
    assert d["mount"] == str(media / "RAK4631")
    assert d["label"] == "RAK4631"
    assert d["info"]["board_id"] == "WisBlock-RAK4631-Board"
    assert d["current_uf2_size"] == 8 * 512


def test_find_uf2_drives_from_proc_mounts_with_space_and_label(tmp_path):
    mnt = make_drive(tmp_path / "run", "L1 PRO 1W", info=INFO_OTAFIX1, current=False)
    dev = tmp_path / "sdc"
    dev.write_text("")
    by_label = tmp_path / "by-label"
    by_label.mkdir()
    (by_label / "L1\\x20PRO\\x201W").symlink_to(dev)
    escaped = str(mnt).replace(" ", "\\040")
    mounts = (
        f"/dev/sda1 / ext4 rw 0 0\n{dev} {escaped} vfat rw,nosuid 0 0\n"
        f"/dev/sdz9 {tmp_path}/nothere vfat rw 0 0\n"
    )
    res = nb.find_uf2_drives(roots=[], mounts=mounts, by_label_dir=by_label, platform="linux")
    assert res["count"] == 1
    d = res["drives"][0]
    assert d["mount"] == str(mnt)
    assert d["device"] == str(dev)
    assert d["label"] == "L1 PRO 1W"
    assert d["current_uf2"] is None
    assert res["fat_mounts_checked"] == 2


def test_find_uf2_drives_lowercase_names_and_dedup(tmp_path):
    d = tmp_path / "vol" / "T1000-E"
    d.mkdir(parents=True)
    (d / "info_uf2.txt").write_text(INFO_OTAFIX1)
    (d / "current.uf2").write_bytes(app_image(1))
    mounts = f"/dev/sdb {d} vfat rw 0 0\n"
    res = nb.find_uf2_drives(roots=[tmp_path / "vol"], mounts=mounts, platform="linux")
    assert res["count"] == 1
    assert res["drives"][0]["current_uf2"].endswith("current.uf2")


def test_default_roots_per_platform(monkeypatch):
    monkeypatch.setenv("USER", "alice")
    assert nb.default_roots("darwin") == [Path("/Volumes")]
    linux = nb.default_roots("linux")
    assert Path("/run/media/alice") in linux and Path("/media/alice") in linux


# --------------------------------------------------------------------------- drive <-> board


def _sysfs(tmp_path: Path, dev_name: str, serial: str) -> Path:
    sys_root = tmp_path / "sys"
    usbdev = sys_root / "devices" / "usb1" / "1-2"
    blk = usbdev / "1-2:1.0" / "host3" / "target3:0:0" / "3:0:0:0" / "block" / dev_name
    blk.mkdir(parents=True)
    (usbdev / "serial").write_text(serial + "\n")
    (usbdev / "idVendor").write_text("239a\n")
    (sys_root / "class" / "block").mkdir(parents=True)
    (sys_root / "class" / "block" / dev_name).symlink_to(blk)
    return sys_root


def test_drive_serial_from_sysfs(tmp_path):
    sys_root = _sysfs(tmp_path, "sdb", "ABCDEF0123456789")
    drive = {"mount": "/x", "device": "/dev/sdb"}
    res = nb.drive_usb_serial(drive, by_id_dir=tmp_path / "none", sys_root=sys_root)
    assert res == {"serial": "ABCDEF0123456789", "method": "sysfs"}


def test_drive_serial_from_by_id(tmp_path):
    dev = tmp_path / "sdb"
    dev.write_text("")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-Mesh_Meshtastic_UF2_ABCDEF0123456789-0:0").symlink_to(dev)
    (by_id / "usb-Other_Disk_999-0:0").symlink_to(tmp_path)
    drive = {"mount": "/x", "device": str(dev)}
    res = nb.drive_usb_serial(drive, by_id_dir=by_id, sys_root=tmp_path / "nosys")
    assert res == {"serial": "ABCDEF0123456789", "method": "by-id"}
    assert nb.drive_usb_serial({"mount": "/x"})["serial"] is None


def test_port_usb_serial_from_listing():
    ports = [
        {"device": "/dev/ttyACM0", "serial_number": "1111"},
        {"device": "/dev/ttyACM1", "serial_number": "abcdef0123456789"},
    ]
    assert nb.port_usb_serial("/dev/ttyACM1", ports) == "abcdef0123456789"
    assert nb.port_usb_serial("/dev/ttyACM9", ports) is None


def test_find_drive_for_port_matches_serial(tmp_path):
    sys_root = _sysfs(tmp_path, "sdb", "ABCDEF0123456789")
    drives = [
        {"mount": "/m/a", "device": "/dev/sdx"},
        {"mount": "/m/b", "device": "/dev/sdb"},
    ]
    ports = [{"device": "/dev/ttyACM1", "serial_number": "abcdef0123456789"}]
    res = nb.find_drive_for_port(
        port="/dev/ttyACM1", drives=drives, ports=ports, by_id_dir=tmp_path, sys_root=sys_root
    )
    assert res["ok"] is True
    assert res["drive"]["mount"] == "/m/b"
    assert res["method"] == "usb_serial_sysfs"
    assert res["verified"] is True


def test_find_drive_for_port_only_drive_fallback(tmp_path):
    drives = [{"mount": "/Volumes/RAK4631", "device": None}]
    res = nb.find_drive_for_port(
        serial_number="ABCDEF0123456789", drives=drives, by_id_dir=tmp_path, sys_root=tmp_path
    )
    assert res["ok"] is True
    assert res["method"] == "only_drive"
    assert res["verified"] is False


def test_find_drive_for_port_refuses_other_boards_drive(tmp_path):
    sys_root = _sysfs(tmp_path, "sdb", "FFFF000000000000")
    drives = [{"mount": "/m/b", "device": "/dev/sdb"}]
    res = nb.find_drive_for_port(
        serial_number="ABCDEF0123456789", drives=drives, by_id_dir=tmp_path, sys_root=sys_root
    )
    assert res["ok"] is False
    assert "ABCDEF0123456789" in res["error"]


def test_find_drive_for_port_ambiguous_and_none(tmp_path):
    two = [{"mount": "/a"}, {"mount": "/b"}]
    res = nb.find_drive_for_port(drives=two, by_id_dir=tmp_path, sys_root=tmp_path)
    assert res["ok"] is False and "2 UF2 drives" in res["error"]
    res = nb.find_drive_for_port(drives=[], by_id_dir=tmp_path, sys_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no UF2 drive mounted"


# --------------------------------------------------------------------------- backup


def test_backup_current_uf2(tmp_path):
    drive = make_drive(tmp_path / "media", "RAK4631")
    out_dir = tmp_path / "backups"
    now = datetime(2026, 11, 16, 9, 30, 5)
    res = nb.backup_current_uf2({"mount": str(drive), "label": "RAK4631"}, out_dir, now=now)
    assert res["ok"] is True
    assert res["path"] == str(
        out_dir / "RAK4631-WisBlock-RAK4631-Board-CURRENT-20261116-093005.uf2"
    )
    assert res["size"] == 8 * 512
    assert res["uf2"]["block_count"] == 8
    again = nb.backup_current_uf2(drive, out_dir, now=now)
    assert again["path"].endswith("-093005-1.uf2")


def test_backup_without_current(tmp_path):
    drive = make_drive(tmp_path / "media", current=False)
    res = nb.backup_current_uf2(drive, tmp_path / "b")
    assert res["ok"] is False and "CURRENT.UF2" in res["error"]


# --------------------------------------------------------------------------- flash


def test_flash_uf2_copies_and_waits_for_detach(tmp_path):
    drive = make_drive(tmp_path / "media")
    fw = tmp_path / "firmware-rak4631.uf2"
    fw.write_bytes(app_image(16))
    clock = FakeClock()
    present = Vanishing(polls=3)
    res = nb.flash_uf2(drive, fw, present=present, sleep=clock.sleep, clock=clock)
    assert res["ok"] is True
    assert res["outcome"] == "detached"
    assert res["bytes_written"] == 16 * 512
    assert (drive / fw.name).read_bytes() == fw.read_bytes()
    assert res["uf2"]["family_id"] == "0xADA52840"
    assert res["wait_s"] == pytest.approx(0.75)


def test_flash_uf2_times_out_when_drive_stays(tmp_path):
    drive = make_drive(tmp_path / "media")
    fw = tmp_path / "fw.uf2"
    fw.write_bytes(app_image(2))
    clock = FakeClock()
    res = nb.flash_uf2(
        drive, fw, timeout_s=2.0, present=lambda _m: True, sleep=clock.sleep, clock=clock
    )
    assert res["ok"] is False
    assert res["outcome"] == "still_present"
    assert "rejected" in res["error"]
    assert clock.t >= 2.0


def test_flash_uf2_refuses_non_uf2(tmp_path):
    drive = make_drive(tmp_path / "media")
    bad = tmp_path / "firmware.bin"
    bad.write_bytes(b"\x7fELF" + bytes(1020))
    res = nb.flash_uf2(drive, bad, present=lambda _m: False)
    assert res["ok"] is False
    assert "bad UF2 magic" in res["error"]
    assert not (drive / bad.name).exists()


def test_flash_uf2_refuses_erase_file_and_non_drive(tmp_path):
    drive = make_drive(tmp_path / "media")
    erase = tmp_path / "erase.uf2"
    erase.write_bytes(nb.factory_erase_uf2_bytes())
    res = nb.flash_uf2(drive, erase, present=lambda _m: False)
    assert res["ok"] is False and "factory_erase()" in res["error"]
    plain = tmp_path / "plain"
    plain.mkdir()
    fw = tmp_path / "fw.uf2"
    fw.write_bytes(app_image(1))
    res = nb.flash_uf2(plain, fw, present=lambda _m: False)
    assert res["ok"] is False and "not a UF2 bootloader drive" in res["error"]


def test_flash_uf2_refuses_file_with_no_acceptable_block(tmp_path):
    drive = make_drive(tmp_path / "media")
    fw = tmp_path / "nofam.uf2"
    fw.write_bytes(uf2_block(0x26000, 0, 1, flags=0))
    res = nb.flash_uf2(drive, fw, present=lambda _m: False)
    assert res["ok"] is False and "no block the bootloader accepts" in res["error"]


def test_flash_uf2_refuses_source_on_drive(tmp_path):
    drive = make_drive(tmp_path / "media")
    res = nb.flash_uf2(drive, drive / "CURRENT.UF2", present=lambda _m: False)
    assert res["ok"] is False and "on the drive itself" in res["error"]


def test_flash_uf2_close_error_after_full_write_is_not_fatal(tmp_path, monkeypatch):
    drive = make_drive(tmp_path / "media")
    fw = tmp_path / "fw.uf2"
    fw.write_bytes(app_image(2))

    def eio(_fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", eio)
    res = nb.flash_uf2(drive, fw, present=lambda _m: False)
    assert res["ok"] is True
    assert res["outcome"] == "detached"
    assert "Input/output error" in res["close_error"]


# --------------------------------------------------------------------------- factory erase


def test_factory_erase_sends_erase_file(tmp_path):
    drive = make_drive(tmp_path / "media")
    erase = tmp_path / "meshtastic_factory_erase.uf2"
    erase.write_bytes(nb.factory_erase_uf2_bytes())
    clock = FakeClock()
    res = nb.factory_erase(drive, erase, present=Vanishing(1), sleep=clock.sleep, clock=clock)
    assert res["ok"] is True
    assert res["outcome"] == "detached"
    assert res["bootloader_version"] == "2.6.1"
    assert (drive / erase.name).read_bytes() == nb.factory_erase_uf2_bytes()


def test_factory_erase_refuses_old_bootloader(tmp_path):
    drive = make_drive(tmp_path / "media", info=INFO_OTAFIX1)
    erase = tmp_path / "e.uf2"
    erase.write_bytes(nb.factory_erase_uf2_bytes())
    res = nb.factory_erase(drive, erase, present=lambda _m: False)
    assert res["ok"] is False
    assert "no Factory-Erase line" in res["error"]
    assert not (drive / erase.name).exists()


def test_factory_erase_refuses_firmware_image(tmp_path):
    drive = make_drive(tmp_path / "media")
    fw = tmp_path / "fw.uf2"
    fw.write_bytes(app_image(2))
    res = nb.factory_erase(drive, fw, present=lambda _m: False)
    assert res["ok"] is False and "not the factory-erase UF2" in res["error"]
    assert not (drive / fw.name).exists()


def test_find_factory_erase_uf2(tmp_path):
    assert nb.find_factory_erase_uf2(None)["ok"] is False
    checkout = tmp_path / "nRF52_Bootloader"
    assert nb.find_factory_erase_uf2(checkout)["ok"] is False
    (checkout / "tools").mkdir(parents=True)
    target = checkout / "tools" / "meshtastic_factory_erase.uf2"
    target.write_bytes(app_image(1))
    assert "not the factory-erase" in nb.find_factory_erase_uf2(checkout)["error"]
    target.write_bytes(nb.factory_erase_uf2_bytes())
    res = nb.find_factory_erase_uf2(checkout)
    assert res["ok"] is True and res["path"] == str(target)
