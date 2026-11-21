# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""BLE targets: the registry learns a radio's BLE identity, and serial and BLE never share it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp import connection, device_names

HELTEC = "/dev/ttyUSB0"
ADDRESS = "48:CA:43:5A:18:C5"
NUM = 0x3A14AA13


@pytest.fixture(autouse=True)
def bench(tmp_path, monkeypatch):
    from serial.tools import list_ports

    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    port = SimpleNamespace(
        device=HELTEC, vid=0x10C4, pid=0xEA60, serial_number="0001", location="1-2.3.1"
    )
    monkeypatch.setattr(list_ports, "comports", lambda: [port])
    monkeypatch.setattr(connection.devices, "list_devices", lambda **_: [{"port": HELTEC}])
    device_names.record_seen(HELTEC, {"node_num": NUM, "model": "HELTEC_V3"})
    connection._ble_held.clear()
    connection._serial_held.clear()


class _FakeBle:
    instances: list = []

    def __init__(self, address=None, noProto=False, timeout=300):
        self.address = address
        self.client = SimpleNamespace(bleak_client=SimpleNamespace(address=ADDRESS))
        self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=NUM)
        self.nodesByNum = {}
        _FakeBle.instances.append(self)

    def close(self):
        pass


@pytest.fixture
def ble(monkeypatch):
    import meshtastic.ble_interface

    _FakeBle.instances = []
    monkeypatch.setattr(meshtastic.ble_interface, "BLEInterface", _FakeBle)
    return _FakeBle


def test_a_ble_connect_records_the_radios_ble_identity(ble) -> None:
    with connection.connect("ble:Meshtastic_18c5"):
        pass
    row = next(r for r in device_names.listing([HELTEC]) if r["port"] == HELTEC)
    assert row["ble_address"] == ADDRESS and row["ble_name"] == "Meshtastic_18c5"


def test_a_device_name_resolves_to_its_ble_address_once_known(ble) -> None:
    with connection.connect("ble:Meshtastic_18c5"):
        pass
    assert connection.resolve_port("ble:heltec-v3-aa13") == f"ble:{ADDRESS}"
    assert connection.resolve_port("ble:Meshtastic_18c5") == "ble:Meshtastic_18c5"


def test_a_serial_open_of_a_radio_held_over_ble_is_refused(ble) -> None:
    with connection.connect("ble:Meshtastic_18c5"):
        with pytest.raises(connection.ConnectionError, match="held over BLE"):
            with connection.connect(HELTEC):
                pass


def test_a_ble_connect_to_a_radio_open_over_serial_is_refused(ble) -> None:
    device_names.record_ble(NUM, ADDRESS, "Meshtastic_18c5")
    connection._serial_held[device_names.key_for_ble(ADDRESS) or ""] = HELTEC
    with pytest.raises(connection.ConnectionError, match="open over serial"):
        with connection.connect(f"ble:{ADDRESS}"):
            pass
    assert ble.instances == []
