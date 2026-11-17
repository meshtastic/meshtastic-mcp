# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""touch_1200bps: how the touch is made and how the bootloader is recognized."""

from __future__ import annotations

import pytest
import serial

from meshtastic_mcp import flash


def _dev(port: str, vid: str, pid: str) -> dict:
    return {"port": port, "vid": vid, "pid": pid, "description": None}


@pytest.fixture
def bench(monkeypatch):
    """Fake pyserial and list_devices: the first listing is `before`, later ones `after`."""
    events: list[tuple] = []
    state: dict = {"before": [], "after": [], "calls": 0}

    class FakeSerial:
        def __init__(self, port, baudrate):
            events.append(("open", port, baudrate))

        @property
        def dtr(self):
            return True

        @dtr.setter
        def dtr(self, value):
            events.append(("dtr", value))

        def close(self):
            events.append(("close",))

    def list_devices(include_unknown: bool = False) -> list:
        state["calls"] += 1
        return list(state["before"] if state["calls"] == 1 else state["after"])

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr(flash.devices, "list_devices", list_devices)
    monkeypatch.setattr(flash.time, "sleep", lambda *a, **k: None)

    def touch(port: str, before: list, after: list) -> dict:
        state["before"], state["after"] = before, after
        return flash.touch_1200bps(port, poll_timeout_s=0.05, retries=1)

    return touch, events


def test_an_nrf52_touch_drops_dtr_at_1200_baud(bench) -> None:
    touch, events = bench
    app = _dev("/dev/ttyACM0", "0x239a", "0x8029")
    touch("/dev/ttyACM0", [app], [_dev("/dev/ttyACM0", "0x239a", "0x002a")])
    assert events == [("open", "/dev/ttyACM0", 1200), ("dtr", False), ("close",)]


def test_a_by_id_link_is_matched_to_its_device_for_the_nrf52_check(bench, tmp_path) -> None:
    touch, events = bench
    device = tmp_path / "ttyACM0"
    device.write_text("")
    link = tmp_path / "usb-RAKwireless_WisCore_RAK4631_Board_7885014CB2C5B186-if00"
    link.symlink_to(device)
    app = _dev(str(device), "0x239a", "0x8029")
    touch(str(link), [app], [_dev(str(device), "0x239a", "0x002a")])
    assert ("dtr", False) in events


def test_an_espressif_port_keeps_the_plain_open_close_touch(bench) -> None:
    touch, events = bench
    s3 = _dev("/dev/ttyACM0", "0x303a", "0x1001")
    touch("/dev/ttyACM0", [s3], [s3])
    assert events == [("open", "/dev/ttyACM0", 1200), ("close",)]


def test_the_rak4631_bootloader_on_the_same_node_is_reported(bench) -> None:
    touch, _ = bench
    app = _dev("/dev/ttyACM0", "0x239a", "0x8029")
    dfu = _dev("/dev/ttyACM0", "0x239a", "0x002a")
    res = touch("/dev/ttyACM0", [app], [dfu])
    assert res["ok"] is True
    assert res["new_port"] == "/dev/ttyACM0"
    assert res["new_port_vid_pid"] == ("0x239a", "0x002a")


def test_a_same_node_pid_change_outside_the_bootloader_table_is_reported(bench) -> None:
    touch, _ = bench
    app = _dev("/dev/ttyACM2", "0x2886", "0x1668")
    other = _dev("/dev/ttyACM1", "0x239a", "0x8029")
    res = touch("/dev/ttyACM2", [other, app], [other, _dev("/dev/ttyACM2", "0x2886", "0x1667")])
    assert res["ok"] is True
    assert res["new_port"] == "/dev/ttyACM2"


def test_a_port_that_comes_back_unchanged_is_not_a_bootloader(bench) -> None:
    touch, _ = bench
    app = _dev("/dev/ttyACM2", "0x2886", "0x1668")
    res = touch("/dev/ttyACM2", [app], [app])
    assert res["ok"] is False
    assert res["new_port"] is None


def test_a_board_that_resets_before_dtr_drops_is_a_successful_touch(monkeypatch) -> None:
    # After a meshtastic session the line can already be DTR-low, so the board resets on the
    # 1200 baud open and the DTR ioctl meets a device that is gone (seen on a RAK4631).
    from meshtastic_mcp import hw_tools

    closed: list[bool] = []

    class VanishingSerial:
        def __init__(self, port, baud):
            pass

        @property
        def dtr(self):
            return True

        @dtr.setter
        def dtr(self, value):
            raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(serial, "Serial", VanishingSerial)
    hw_tools.touch_1200("/dev/ttyFAKE0", drop_dtr=True)
    assert closed == [True]
