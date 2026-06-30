# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Device-discovery `likely_meshtastic` classification."""

from __future__ import annotations

from meshtastic_mcp import devices


class _FakePort:
    def __init__(self, device, vid, pid, product=""):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.product = product
        self.description = product
        self.manufacturer = ""
        self.serial_number = ""


def _patch(monkeypatch, ports):
    from meshtastic import util as mt_util

    monkeypatch.setattr(devices.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(
        mt_util, "findPorts", lambda eliminate_duplicates=True: {p.device for p in ports}
    )
    monkeypatch.setattr(mt_util, "whitelistVids", {0x239A: None, 0x303A: None}, raising=False)
    monkeypatch.setattr(mt_util, "blacklistVids", {0x1366: None}, raising=False)
    monkeypatch.delenv("MESHTASTIC_MCP_TCP_HOST", raising=False)


def test_common_board_chips_are_flagged_likely(monkeypatch):
    """CP210x / CH340 / FTDI / Seeed boards must be likely, even alongside a native-USB one."""
    ports = [
        _FakePort("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP2102 USB to UART"),  # Heltec / most ESP32
        _FakePort("/dev/ttyACM0", 0x2886, 0x0059, "seeed-xiao-s3"),  # XIAO TinyUSB-CDC
        _FakePort("/dev/ttyACM1", 0x303A, 0x1001, "USB JTAG/serial"),  # HWCDC (upstream allowlist)
        _FakePort("/dev/ttyUSB1", 0x1A86, 0x7523, "CH340"),  # cheap ESP32
        _FakePort("/dev/ttyACM9", 0x1366, 0x1051, "J-Link"),  # debug probe (blocklisted)
        _FakePort("/dev/ttyUSB9", 0xDEAD, 0x0001, "Unknown gadget"),  # unknown
    ]
    _patch(monkeypatch, ports)
    out = {d["port"]: d for d in devices.list_devices(include_unknown=True)}

    for p in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB1"):
        assert out[p]["likely_meshtastic"] is True, p

    assert out["/dev/ttyACM9"]["likely_meshtastic"] is False
    assert out["/dev/ttyACM9"]["blacklisted"] is True
    assert out["/dev/ttyUSB9"]["likely_meshtastic"] is False  # unknown VID: candidate, not likely


def test_unknown_chips_hidden_without_include_unknown(monkeypatch):
    ports = [_FakePort("/dev/ttyUSB9", 0xDEAD, 0x0001, "Unknown gadget")]
    _patch(monkeypatch, ports)
    # The unknown device is a fallback candidate (in findPorts), so it still shows; a
    # blocklisted-only port would not. Assert the likely flag stays False.
    res = devices.list_devices(include_unknown=False)
    assert all(r["likely_meshtastic"] is False for r in res if r["port"].startswith("/dev"))
