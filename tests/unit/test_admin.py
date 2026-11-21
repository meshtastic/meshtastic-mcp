# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`admin`: per-action confirmation, node-DB actions, and the wait for a restarted radio."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2

from meshtastic_mcp import backends, confirm, connection
from meshtastic_mcp.backends import python_admin
from meshtastic_mcp.tools import core

ME = 0x3A14AA13


class _Radio:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.uptime = 1000
        self.restart_pending = False
        self.connects = 0

    def iface(self):
        self.connects += 1
        if self.restart_pending:
            self.uptime, self.restart_pending = 2, False
        radio = self

        class Node:
            def __getattr__(self, name):
                def call(*args, **kwargs):
                    radio.calls.append((name, args, kwargs))
                    if name in ("reboot", "factoryReset", "resetNodeDb"):
                        radio.restart_pending = True

                return call

        def answer(request, *, destinationId, portNum, wantResponse, onResponse):
            onResponse(
                {"decoded": {"telemetry": {"deviceMetrics": {"uptimeSeconds": self.uptime}}}}
            )

        return SimpleNamespace(
            devPath="/dev/ttyUSB0",
            localNode=Node(),
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=ME),
            sendData=answer,
        )


@pytest.fixture
def radio(monkeypatch):
    r = _Radio()

    @contextmanager
    def fake_connect(port=None, timeout_s=8.0, linger_s=0.0):
        yield r.iface()

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(python_admin.time, "sleep", lambda s: None)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    return r


def _run(**kwargs):
    return asyncio.run(core.admin(None, **kwargs))


def test_a_restart_waits_for_the_radio_and_sees_the_new_uptime(radio) -> None:
    result = _run(action="reboot", confirm=True)
    assert radio.calls == [("reboot", (), {"secs": 3})]
    assert result["back"] is True and result["restarted"] is True and result["uptime_s"] == 2
    assert radio.connects == 2


def test_a_restart_without_waiting_returns_at_once(radio) -> None:
    result = _run(action="reboot", confirm=True, wait_back=False)
    assert "back" not in result and radio.connects == 1


def test_a_restarting_action_needs_confirmation(radio, monkeypatch) -> None:
    seen = []

    async def refuse(ctx, what, confirmed, irreversible=False):
        seen.append((what, irreversible))
        return {"ok": False, "needs_confirmation": what}

    monkeypatch.setattr(confirm, "gate", refuse)
    result = _run(action="factory_reset_full")
    assert result["ok"] is False and radio.calls == []
    assert seen == [("wipe the default radio: settings, node DB, keys and identity", True)]


def test_a_node_db_action_names_its_node_and_asks_nothing(radio, monkeypatch) -> None:
    async def never(*args, **kwargs):
        raise AssertionError("a node-DB action is not gated")

    monkeypatch.setattr(confirm, "gate", never)
    result = _run(action="favorite", node_id="!c2c91376")
    assert radio.calls == [("setFavorite", ("!c2c91376",), {})]
    assert result["node_id"] == "!c2c91376" and "back" not in result


def test_a_node_db_action_without_a_node_is_refused(radio) -> None:
    with pytest.raises(ToolError, match="node_id"):
        _run(action="ignore")


@pytest.mark.parametrize(
    "error",
    [
        connection.ConnectionError("Serial open failed"),
        MeshInterface.MeshInterfaceError("Timed out waiting for connection completion"),
    ],
)
def test_a_radio_that_does_not_come_back_is_reported(radio, monkeypatch, error) -> None:
    real = connection.connect
    calls = {"n": 0}

    @contextmanager
    def flaky(port=None, timeout_s=8.0, linger_s=0.0):
        calls["n"] += 1
        if calls["n"] > 1:
            raise error
        with real(port=port) as iface:
            yield iface

    monkeypatch.setattr(connection, "connect", flaky)
    result = _run(action="reboot", confirm=True, timeout_s=0.01)
    assert result["back"] is False and "did not answer again" in result["error"]


@pytest.mark.parametrize("action", ["factory_reset", "factory_reset_full", "reset_nodedb"])
def test_a_reset_waits_out_the_firmware_restart_timer_before_reconnecting(
    radio, monkeypatch, action
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(python_admin.time, "sleep", slept.append)
    _run(action=action, confirm=True, timeout_s=60)
    assert sum(slept) >= python_admin.REBOOT_DELAY_S + python_admin.BOOT_S
