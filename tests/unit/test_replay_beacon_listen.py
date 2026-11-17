# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`beacon_listen` reaches the replay device from every entry point that starts one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from meshtastic.protobuf import mesh_pb2, module_config_pb2

from meshtastic_mcp.replay import ReplayParams, ReplaySession, build, sim

_LISTEN = module_config_pb2.ModuleConfig.MeshBeaconConfig.FLAG_LISTEN_ENABLED


def _beacon_configs(beacon_listen: bool) -> list[Any]:
    cap = sim.generate(nodes=3, days=1, seed=2, start=1_700_000_000)
    params = ReplayParams(host="127.0.0.1", port=0, beacon_listen=beacon_listen)
    frames: list[mesh_pb2.FromRadio] = []
    ReplaySession("beacon", cap, params)._send_config_phase(frames.append)
    return [
        f.moduleConfig.mesh_beacon
        for f in frames
        if f.HasField("moduleConfig")
        and f.moduleConfig.WhichOneof("payload_variant") == "mesh_beacon"
    ]


def test_beacon_listen_config_is_the_generated_mesh_beacon_message():
    assert not hasattr(build, "mesh_beacon_module_config")
    assert [c.flags for c in _beacon_configs(True)] == [_LISTEN]
    assert _beacon_configs(False) == []


def test_replay_start_forwards_beacon_listen(monkeypatch):
    from meshtastic_mcp import server

    cap = sim.generate(nodes=2, days=1, seed=1, start=1_700_000_000)
    started: list[ReplayParams] = []

    class Manager:
        def start(self, capture, params):
            started.append(params)
            return {"id": "test"}

    monkeypatch.setattr("meshtastic_mcp.tools.replay._load_replay_capture", lambda *_a, **_k: cap)
    monkeypatch.setattr("meshtastic_mcp.tools.replay.get_replay_manager", lambda: Manager())

    server.replay_start(beacon_listen=True, mdns=False)
    server.replay_start(mdns=False)
    assert [p.beacon_listen for p in started] == [True, False]


def _cli_args(monkeypatch, *flags: str):
    from meshtastic_mcp import __main__ as cli

    seen = []

    def fake_cmd_replay(args):
        seen.append(args)
        return 0

    monkeypatch.setattr(cli, "_cmd_replay", fake_cmd_replay)
    with pytest.raises(SystemExit) as exc:
        cli.main(["replay", "--nodes", "3", "--days", "1", "--port", "0", "--no-mdns", *flags])
    assert exc.value.code == 0
    return cli._build_replay_session(seen[0])


def test_cli_replay_beacon_listen_flag(monkeypatch):
    assert _cli_args(monkeypatch, "--beacon-listen").params.beacon_listen is True
    assert _cli_args(monkeypatch).params.beacon_listen is False


class _Driver:
    name = "android"
    app = "org.meshtastic.debug"

    def set_location(self, lat: float, lon: float) -> None: ...
    def clear_location(self) -> None: ...
    def connect(self, port: int) -> None: ...
    def appearance(self, mode: str) -> None: ...
    def open(self, path: str) -> None: ...
    def reset_status_bar(self) -> None: ...

    def wait_ready(self) -> bool:
        return True

    def screenshot(self, out: Path) -> None:
        out.write_bytes(b"png")


def test_screenshot_set_serves_beacon_listen(monkeypatch, tmp_path):
    from meshtastic_mcp import replay
    from meshtastic_mcp import screenshot_set as ss

    started: list[ReplayParams] = []

    class Manager:
        def start(self, capture, params):
            started.append(params)
            return {"id": "s1", "listen": "0.0.0.0:4403"}

        def status(self, sid):
            return {"connected": True, "pass_complete": True}

        def stop(self, sid):
            return {}

    monkeypatch.setattr(ss, "AndroidDriver", lambda serial: _Driver())
    monkeypatch.setattr(replay, "get_manager", lambda: Manager())
    plan = ss.make_plan(
        ["nodes"],
        ["android"],
        ["light"],
        tmp_path,
        sim_nodes=3,
        settle_s=0.0,
        clean_status_bar=False,
        beacon_listen=True,
    )
    ss._run(plan, {"artifacts": []}, tmp_path / "log.txt")
    assert [p.beacon_listen for p in started] == [True]
