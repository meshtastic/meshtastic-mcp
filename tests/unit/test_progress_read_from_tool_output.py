# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Progress is read from what pio, esptool, picotool and nrfutil print, and from a UF2 copy."""

from __future__ import annotations

import pytest

from meshtastic_mcp import jobs, nrf52_bootloader, pio


class _Recorder(jobs.Progress):
    """Keeps every update, where the waiter only samples the latest."""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[str, float | None, float | None]] = []

    def update(self, message: str, done: float | None = None, total: float | None = None) -> None:
        super().update(message, done, total)
        self.updates.append((message, done, total))


def _read(*lines: str) -> list[tuple[str, float | None, float | None]]:
    progress = _Recorder()
    reader = pio.OutputProgress(progress)
    for line in lines:
        reader.feed(line)
    return progress.updates


def test_esptool_v5_counts_bytes_across_every_image_it_announced() -> None:
    updates = _read(
        "Flash will be erased from 0x00001000 to 0x00004fff...",
        "Writing at 0x00003000 [=====     ]  50.0% 8.0kB/16.0kB [00:00]",
        "Writing at 0x00005000 [==========] 100.0% 16.0kB/16.0kB [00:00]",
        "Flash will be erased from 0x00010000 to 0x0001ffff...",
        "Writing at 0x00014000 [==        ]  25.0% 16.0kB/64.0kB [00:01]",
    )
    measured = [(done, total) for _m, done, total in updates if done is not None]
    assert measured == [(0x2000, 0x4000), (0x4000, 0x4000), (0x4000 + 0x4000, 0x14000)]
    assert updates[1][0] == "Writing at 0x00003000 (50.0%)"


def test_esptool_v4_announces_every_range_first_and_prints_before_each_block() -> None:
    updates = _read(
        "Flash will be erased from 0x00001000 to 0x00004fff...",
        "Flash will be erased from 0x00008000 to 0x00008fff...",
        "Compressed 16384 bytes to 9000...",
        "Writing at 0x00001000... (50 %)",
        "Writing at 0x00003000... (100 %)",
        "Compressed 3072 bytes to 100...",
        "Writing at 0x00008000... (100 %)",
    )
    measured = [(done, total) for _m, done, total in updates if done is not None]
    assert measured == [(0x2000, 0x5000), (0x4000, 0x5000), (0x5000, 0x5000)]
    assert updates[3][0] == "Writing at 0x00001000 (50%)"


def test_a_write_outside_any_announced_range_counts_percent_per_image() -> None:
    updates = _read(
        "Writing at 0x00010000... (40 %)",
        "Writing at 0x00014000... (100 %)",
        "Writing at 0x00260000... (30 %)",
    )
    measured = [(done, total) for _m, done, total in updates if done is not None]
    assert measured == [(40, 100), (100, 100), (130, 200)]


def test_picotool_bars_are_progress() -> None:
    updates = _read("Loading into Flash: [==============                ]  45%")
    assert updates == [("Loading into Flash (45%)", 45.0, 100.0)]


def test_nrfutil_hashes_count_packets_without_a_total() -> None:
    updates = _read(
        "Upgrading target on /dev/ttyACM0 with DFU package /tmp/fw.zip.",
        "#" * 40,
        "#" * 40,
        "#####",
    )
    assert updates[-1] == ("DFU: 85 packets sent", 85, None)


@pytest.mark.parametrize(
    "line",
    [
        "RAM:   [==        ]  15.2% (used 49820 bytes from 327680 bytes)",
        "Flash: [=====     ]  52.3% (used 1645317 bytes from 3145728 bytes)",
        "Compiling .pio/build/tbeam/src/main.cpp.o",
        "[nanopb] No generation needed.",
        "========== [SUCCESS] Took 32.98 seconds ==========",
    ],
)
def test_other_lines_are_messages_only(line) -> None:
    assert _read(line) == [(line, None, None)]


@pytest.mark.parametrize(
    "line",
    [
        "-" * 60,
        "{'name': 'firmware-rak4631-2.8.3.68f47ef-ota.zip', 'md5': '0f3a'}",
        '[["env:rak4631", [["board", "wiscore_rak4631"]]]]',
    ],
)
def test_separator_and_dump_lines_are_not_messages(line) -> None:
    assert _read(line) == []


def test_a_verified_esptool_image_counts_as_fully_written() -> None:
    updates = _read(
        "Flash will be erased from 0x00010000 to 0x00238fff...",
        "Writing at 0x002325e7 [========= ]  99.0% 2237927/2265088 bytes",
        "Hash of data verified.",
    )
    measured = [(done, total) for _m, done, total in updates if done is not None]
    assert measured[-1] == (0x229000, 0x229000)


def test_blank_lines_and_terminal_codes_are_dropped() -> None:
    assert _read("", "   ", "\x1b[32mLinking .pio/build/tbeam/firmware.elf\x1b[0m") == [
        ("Linking .pio/build/tbeam/firmware.elf", None, None)
    ]


def test_a_uf2_copy_reports_the_bytes_it_copied(tmp_path) -> None:
    src = tmp_path / "firmware.uf2"
    src.write_bytes(b"\0" * (3 * 64 * 1024 + 100))
    dest = tmp_path / "drive" / "firmware.uf2"
    dest.parent.mkdir()
    progress = _Recorder()

    with jobs.tracking(progress):
        written, err = nrf52_bootloader._copy_to_drive(src, dest, src.stat().st_size)

    size = src.stat().st_size
    assert err is None and written == size
    measured = [(done, total) for _m, done, total in progress.updates]
    assert measured[0] == (64 * 1024, size)
    assert measured[-1] == (size, size)
    assert progress.updates[-1][0] == "syncing firmware.uf2 to the drive"


def test_report_off_a_tracked_thread_is_a_no_op() -> None:
    jobs.report("nobody is listening", 1, 2)
