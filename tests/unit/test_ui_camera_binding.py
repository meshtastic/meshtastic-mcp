# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Unit tests for the UI tier's DB-backed camera resolver
(`tests.ui.conftest.camera_binding_for_role`): role → bench hub slot → the
device on that slot → the camera FleetSuite assigned to it. Seeds a temp
registry DB (no hardware, no real fleetsuite.db) via MESHTASTIC_MCP_WEB_DB.
"""

from __future__ import annotations

import sqlite3

from tests.ui.conftest import camera_binding_for_role


def _seed_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE devices (
            serial_number TEXT PRIMARY KEY,
            hub_location TEXT,
            hub_port INTEGER
        );
        CREATE TABLE cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            device_index TEXT,
            rotation INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            device_serial TEXT,
            mirror INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    # t_echo's bench slot is 20-3.1 (see tests/_bench.py).
    con.execute(
        "INSERT INTO devices (serial_number, hub_location, hub_port) VALUES (?,?,?)",
        ("SER_TECHO", "20-3", 1),
    )
    con.execute(
        "INSERT INTO cameras (name, device_index, rotation, enabled, device_serial, mirror) "
        "VALUES (?,?,?,?,?,?)",
        ("HD USB Camera", "0", 180, 1, "SER_TECHO", 0),
    )
    con.commit()
    con.close()


def test_binding_resolves_index_rotation_mirror(tmp_path, monkeypatch):
    db = tmp_path / "fleetsuite.db"
    _seed_db(str(db))
    monkeypatch.setenv("MESHTASTIC_MCP_WEB_DB", str(db))

    binding = camera_binding_for_role("t_echo")
    assert binding == {"device_index": "0", "rotation": 180, "mirror": False}


def test_binding_none_for_unbound_slot(tmp_path, monkeypatch):
    db = tmp_path / "fleetsuite.db"
    _seed_db(str(db))
    monkeypatch.setenv("MESHTASTIC_MCP_WEB_DB", str(db))

    # esp32s3's slot (20-3.5) has no device/camera seeded → no binding.
    assert camera_binding_for_role("esp32s3") is None


def test_binding_none_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_WEB_DB", str(tmp_path / "does-not-exist.db"))
    assert camera_binding_for_role("t_echo") is None


def test_binding_skips_disabled_camera(tmp_path, monkeypatch):
    db = tmp_path / "fleetsuite.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE devices (serial_number TEXT PRIMARY KEY, hub_location TEXT, hub_port INTEGER);
        CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            device_index TEXT, rotation INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
            device_serial TEXT, mirror INTEGER NOT NULL DEFAULT 0);
        """
    )
    con.execute("INSERT INTO devices (serial_number, hub_location, hub_port) VALUES ('S','20-3',1)")
    con.execute(
        "INSERT INTO cameras (name, device_index, rotation, enabled, device_serial) "
        "VALUES ('cam','0',0,0,'S')"  # enabled=0
    )
    con.commit()
    con.close()
    monkeypatch.setenv("MESHTASTIC_MCP_WEB_DB", str(db))

    assert camera_binding_for_role("t_echo") is None
