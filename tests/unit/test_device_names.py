# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""The device registry: stable names, resolution in `resolve_port`, and `devices()`."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from meshtastic_mcp import connection, device_names, leases

RAK_APP = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_7885014CB2C5B186-if00"
RAK_DFU = "/dev/serial/by-id/usb-RAKWireless_WisBlock_RAK4631_7885014CB2C5B186-if00"
HELTEC = "/dev/ttyUSB0"


def _rak(device: str, pid: int = 0x8029):
    return SimpleNamespace(
        device=device, vid=0x239A, pid=pid, serial_number="7885014CB2C5B186", location="1-2.3.3"
    )


def _heltec():
    return SimpleNamespace(
        device=HELTEC, vid=0x10C4, pid=0xEA60, serial_number="0001", location="1-2.3.1"
    )


@pytest.fixture(autouse=True)
def bench(tmp_path, monkeypatch):
    from serial.tools import list_ports

    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    ports = [_rak(RAK_APP), _heltec()]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    monkeypatch.setattr(
        connection.devices, "list_devices", lambda **_: [{"port": p.device} for p in ports]
    )
    return ports


def _seen(port, num, model="RAK4631"):
    return device_names.record_seen(
        port, {"node_num": num, "model": model, "long_name": "x", "short_name": "y"}
    )


def test_a_radio_is_named_on_its_first_connect_and_keeps_the_name(bench) -> None:
    assert _seen(RAK_APP, 0x7885B186)["name"] == "rak4631-b186"
    # It comes back from DFU under another path and PID; same radio, same name.
    bench[0] = _rak(RAK_DFU, pid=0x0029)
    assert _seen(RAK_DFU, 0x7885B186)["name"] == "rak4631-b186"


def test_a_name_collision_gets_a_suffix() -> None:
    assert _seen(RAK_APP, 0x0000AA13, "HELTEC_V3")["name"] == "heltec-v3-aa13"
    assert _seen(HELTEC, 0x1111AA13, "HELTEC_V3")["name"] == "heltec-v3-aa13-2"


def test_resolve_port_takes_a_name_with_or_without_this_host() -> None:
    _seen(HELTEC, 0x3A14AA13, "HELTEC_V3")
    assert connection.resolve_port("heltec-v3-aa13") == HELTEC
    assert connection.resolve_port(f"{socket.gethostname()}/heltec-v3-aa13") == HELTEC
    assert connection.resolve_port(RAK_APP) == RAK_APP


def test_a_known_radio_that_is_unplugged_is_a_clear_error(bench) -> None:
    _seen(HELTEC, 0x3A14AA13, "HELTEC_V3")
    bench.pop()
    with pytest.raises(connection.ConnectionError, match="not attached"):
        connection.resolve_port("heltec-v3-aa13")


def test_rename_is_stable_and_refuses_a_taken_name() -> None:
    _seen(HELTEC, 0x3A14AA13, "HELTEC_V3")
    _seen(RAK_APP, 0x7885B186)
    assert device_names.rename("heltec-v3-aa13", "Bench Heltec")["name"] == "bench-heltec"
    assert connection.resolve_port("bench-heltec") == HELTEC
    with pytest.raises(ValueError, match="already named"):
        device_names.rename(RAK_APP, "bench-heltec")
    _seen(HELTEC, 0x3A14AA13, "HELTEC_V3")
    assert connection.resolve_port("bench-heltec") == HELTEC


def test_listing_shows_attached_unnamed_and_unplugged_radios_and_leases(bench) -> None:
    _seen(HELTEC, 0x3A14AA13, "HELTEC_V3")
    leases.claim(HELTEC, holder="eval")
    rows = {r["port"]: r for r in device_names.listing([p.device for p in bench])}
    assert rows[HELTEC]["name"] == f"{socket.gethostname()}/heltec-v3-aa13"
    assert rows[HELTEC]["leased_to"] == "eval"
    assert rows[RAK_APP]["name"] is None and rows[RAK_APP]["attached"] is True
    gone = device_names.listing([RAK_APP])
    assert any(r["port"] == HELTEC and r["attached"] is False for r in gone)


def test_connect_records_what_it_saw() -> None:
    iface = SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=0x3A14AA13),
        nodesByNum={0x3A14AA13: {"user": {"hwModel": "HELTEC_V3", "longName": "Meshtastic 18c4"}}},
    )
    connection._remember(HELTEC, iface)
    row = next(r for r in device_names.listing([HELTEC]) if r["port"] == HELTEC)
    assert row["model"] == "HELTEC_V3" and row["long_name"] == "Meshtastic 18c4"
    connection._remember(HELTEC, object())  # an interface with nothing on it never raises


def test_ports_and_com_ports_are_not_names() -> None:
    for value in ("/dev/ttyUSB0", "tcp://host:4403", "COM3", "com12"):
        assert not device_names.looks_like_name(value)
    assert device_names.looks_like_name("heltec-v3-aa13")
