# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""meshtastic-mcp-atak: argparse wiring → library calls (no emulator)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meshtastic_mcp import atak_cli
from meshtastic_mcp.emulator import atak


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """Replace every library entry point the CLI touches with a recorder."""
    rec: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def fake(name: str, ret: Any = None) -> Any:
        def _f(*a: Any, **kw: Any) -> Any:
            rec.append((name, a, kw))
            return ret

        return _f

    fleet = atak.Fleet(
        nodes=[atak.FleetNode(name="atak-node-0", serial="emulator-5554", port=5554)]
    )
    monkeypatch.setattr(atak, "fleet_up", fake("fleet_up", fleet))
    monkeypatch.setattr(atak, "fleet_down", fake("fleet_down"))
    monkeypatch.setattr(atak, "discover_fleet", fake("discover_fleet", fleet))
    monkeypatch.setattr(atak, "list_clone_avds", fake("list_clone_avds", ["atak-node-1"]))
    monkeypatch.setattr(atak, "delete_clone_avd", fake("delete_clone_avd"))
    monkeypatch.setattr(atak, "drive_route", fake("drive_route"))
    monkeypatch.setattr(atak, "set_position", fake("set_position"))
    monkeypatch.setattr(atak, "provision", fake("provision"))
    return rec


def test_fleet_up_wiring(calls: list[Any], capsys: pytest.CaptureFixture[str]) -> None:
    rc = atak_cli.main(
        ["fleet", "up", "--count", "2", "--apk", "a.apk", "--base-avd", "mp", "--relay-port", "9"]
    )
    assert rc == 0
    assert calls == [
        ("fleet_up", (2, "a.apk"), {"base_avd": "mp", "relay_port": 9, "use_snapshot": True})
    ]
    assert '"serial": "emulator-5554"' in capsys.readouterr().out


def test_fleet_up_no_snapshot(calls: list[Any]) -> None:
    atak_cli.main(["fleet", "up", "--count", "1", "--apk", "a", "--base-avd", "b", "--no-snapshot"])
    assert calls[0][2]["use_snapshot"] is False
    assert calls[0][2]["relay_port"] == 8087


def test_fleet_down_discovers_running_nodes(calls: list[Any]) -> None:
    assert atak_cli.main(["fleet", "down"]) == 0
    assert [c[0] for c in calls] == ["discover_fleet", "fleet_down"]
    assert calls[1][2] == {"delete_clones": False}


def test_fleet_down_delete_clones_sweeps_stopped_clones(calls: list[Any]) -> None:
    assert atak_cli.main(["fleet", "down", "--delete-clones"]) == 0
    assert [c[0] for c in calls] == [
        "discover_fleet",
        "fleet_down",
        "list_clone_avds",
        "delete_clone_avd",
    ]
    assert calls[1][2] == {"delete_clones": True}
    assert calls[3][1] == ("atak-node-1",)


def test_drive_wiring(calls: list[Any]) -> None:
    rc = atak_cli.main(
        ["drive", "emulator-5554", "--speed", "3.5", "--step", "1", "1.5,2.5", "-3,4.25"]
    )
    assert rc == 0
    assert calls == [
        (
            "drive_route",
            ("emulator-5554", [(1.5, 2.5), (-3.0, 4.25)]),
            {"speed_mps": 3.5, "step_s": 1.0},
        )
    ]


def test_drive_rejects_bad_waypoint(calls: list[Any]) -> None:
    with pytest.raises(SystemExit) as exc:
        atak_cli.main(["drive", "emulator-5554", "1.5", "2,3"])
    assert exc.value.code == 2
    assert calls == []


def test_position_wiring(calls: list[Any]) -> None:
    assert atak_cli.main(["position", "emulator-5554", "1", "2"]) == 0
    assert calls == [("set_position", ("emulator-5554", 1.0, 2.0), {})]
    calls.clear()
    assert atak_cli.main(["position", "emulator-5554", "1", "2", "--speed", "4"]) == 0
    assert calls == [("set_position", ("emulator-5554", 1.0, 2.0), {"speed_mps": 4.0})]


def test_provision_wiring(calls: list[Any]) -> None:
    assert atak_cli.main(["provision", "emulator-5556", "--apk", "x.apk", "--relay-port", "1"]) == 0
    assert calls == [("provision", ("emulator-5556", "x.apk"), {"relay_port": 1})]


def test_atak_error_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: Any, **kw: Any) -> None:
        raise atak.AtakError("no free emulator console port")

    monkeypatch.setattr(atak, "provision", boom)
    assert atak_cli.main(["provision", "s", "--apk", "x"]) == 1
    assert "no free emulator console port" in capsys.readouterr().err


def test_relay_start_uses_session_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(atak_cli.config, "mcp_data_dir", lambda: tmp_path)
    monkeypatch.setattr(atak_cli, "_STATUS_EVERY_S", 0.01)
    started: dict[str, Any] = {}

    def fake_sleep(_s: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(atak_cli.time, "sleep", fake_sleep)

    class FakeRelay:
        host = "0.0.0.0"

        def __init__(self, *, outdir: Path, port: int) -> None:
            started["outdir"], started["port"] = outdir, port
            self.port = port

        def start(self) -> int:
            started["started"] = True
            return self.port

        def stop(self) -> None:
            started["stopped"] = True

        def status(self) -> dict[str, Any]:
            return {"peers": [], "peer_count": 0, "type_counts": {}}

    monkeypatch.setattr(atak_cli, "CotRelay", FakeRelay)
    assert atak_cli.main(["relay", "start", "--port", "0", "--session", "s1"]) == 0
    assert started == {
        "outdir": tmp_path / "cot_captures" / "s1",
        "port": 0,
        "started": True,
        "stopped": True,
    }
    assert str(tmp_path / "cot_captures" / "s1") in capsys.readouterr().out


# ---------------------------------------------------------------------------
# atak.discover_fleet / list_clone_avds (library side of `fleet down`)
# ---------------------------------------------------------------------------
def test_discover_fleet_filters_by_avd_name(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {"emulator-5554": "atak-node-0\nOK\n", "emulator-5556": "Pixel_7\nOK\n"}
    monkeypatch.setattr(
        atak.avd,
        "list_devices",
        lambda: [("emulator-5554", "device"), ("emulator-5556", "device"), ("ABC123", "device")],
    )

    class _CP:
        def __init__(self, out: str) -> None:
            self.stdout = out

    monkeypatch.setattr(atak.avd, "adb", lambda *a, serial=None, **kw: _CP(names[serial]))
    fleet = atak.discover_fleet()
    assert [(n.name, n.serial, n.port) for n in fleet.nodes] == [
        ("atak-node-0", "emulator-5554", 5554)
    ]


def test_list_clone_avds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atak, "_avd_home", lambda: tmp_path)
    for n in ("atak-node-1", "atak-node-0", "medium_phone"):
        (tmp_path / f"{n}.ini").write_text("x")
    assert atak.list_clone_avds() == ["atak-node-0", "atak-node-1"]
