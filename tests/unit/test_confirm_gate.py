# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`reboot` asks the human when the client can ask, and keeps the `confirm=True` contract otherwise."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from mcp.types import ElicitResult

from meshtastic_mcp import confirm


def _record_admin(monkeypatch, calls: list[dict], keys: tuple[str, ...]) -> None:
    """The legacy reboot and factory_reset tools reach the radio through the backend."""
    from meshtastic_mcp import backends
    from meshtastic_mcp.backends.python import PythonBackend

    def fake_admin(self, target, action, node_id, seconds, wait_back, timeout_s, *rest):
        seen = {"port": target, "confirm": True, "full": action.endswith("_full")}
        calls.append({k: seen[k] for k in keys})
        return {"target": target, "action": action, "ok": True}

    monkeypatch.setattr(PythonBackend, "admin", fake_admin)
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)


@pytest.fixture
def reboots(monkeypatch):
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server

    calls: list[dict] = []
    _record_admin(monkeypatch, calls, ("port", "confirm"))
    monkeypatch.delenv(confirm_env(), raising=False)
    return calls, server


def confirm_env() -> str:
    return confirm.POLICY_ENV


def _call(server, args, handler=None, mode=None):
    kwargs = {"mode": mode} if mode else {}

    async def run():
        async with Client(server.app, elicitation_handler=handler, **kwargs) as client:
            return await client.call_tool("reboot", args, raise_on_error=False)

    return asyncio.run(run())


def _answer(action: str, value: bool = True):
    seen: list[str] = []

    async def handler(message, response_type, params, context):
        seen.append(message)
        # `confirm` answers the modern form; `value` answers the legacy scalar wrapper.
        content = {"confirm": value, "value": value} if action == "accept" else None
        return ElicitResult(action=action, content=content)

    return handler, seen


def test_confirm_true_proceeds_without_asking(reboots) -> None:
    calls, server = reboots
    handler, seen = _answer("accept")
    result = _call(server, {"confirm": True}, handler)
    assert result.is_error is False and calls == [{"port": None, "confirm": True}]
    assert seen == []


def test_missing_confirm_asks_the_human_and_proceeds_on_accept(reboots) -> None:
    calls, server = reboots
    handler, seen = _answer("accept")
    result = _call(server, {}, handler)
    assert result.is_error is False
    assert len(seen) == 1 and "reboot" in seen[0]
    assert calls == [{"port": None, "confirm": True}]


def test_declining_refuses_and_does_not_reboot(reboots) -> None:
    calls, server = reboots
    handler, _ = _answer("decline")
    result = _call(server, {}, handler)
    assert result.is_error is True
    assert calls == []


def test_answering_false_refuses_and_does_not_reboot(reboots) -> None:
    calls, server = reboots
    handler, _ = _answer("accept", value=False)
    assert _call(server, {}, handler).is_error is True
    assert calls == []


@pytest.mark.parametrize(
    ("action", "value", "rebooted"), [("accept", True, True), ("decline", True, False)]
)
def test_handshake_era_clients_are_asked_through_elicit(reboots, action, value, rebooted) -> None:
    calls, server = reboots
    handler, seen = _answer(action, value)
    result = _call(server, {}, handler, mode="legacy")
    assert len(seen) == 1
    assert (result.is_error is False) is rebooted
    assert bool(calls) is rebooted


def test_a_client_that_cannot_ask_keeps_the_confirm_contract(reboots) -> None:
    calls, server = reboots
    result = _call(server, {})
    assert result.is_error is True and "confirm=True" in result.content[0].text
    assert calls == []


@pytest.fixture
def resets(monkeypatch):
    from meshtastic_mcp import server

    calls: list[dict] = []
    _record_admin(monkeypatch, calls, ("confirm", "full"))
    monkeypatch.setenv(confirm.POLICY_ENV, "agent")
    return calls, server


def _reset(server, args, handler=None, mode=None):
    kwargs = {"mode": mode} if mode else {}

    async def run():
        async with Client(server.app, elicitation_handler=handler, **kwargs) as client:
            return await client.call_tool("factory_reset", args, raise_on_error=False)

    return asyncio.run(run())


def test_agent_policy_lets_confirm_true_reset_without_asking(resets) -> None:
    calls, server = resets
    handler, seen = _answer("accept")
    assert _reset(server, {"confirm": True}, handler).is_error is False
    assert seen == [] and calls == [{"confirm": True, "full": False}]


@pytest.mark.parametrize("mode", [None, "legacy"])
def test_human_policy_asks_even_when_confirm_is_true(resets, monkeypatch, mode) -> None:
    calls, server = resets
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, seen = _answer("accept")
    assert _reset(server, {"confirm": True, "full": True}, handler, mode).is_error is False
    assert len(seen) == 1 and "wipe its identity" in seen[0]
    assert calls == [{"confirm": True, "full": True}]


@pytest.mark.parametrize("mode", [None, "legacy"])
def test_human_policy_refuses_when_the_human_declines(resets, monkeypatch, mode) -> None:
    calls, server = resets
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, _ = _answer("decline")
    assert _reset(server, {"confirm": True}, handler, mode).is_error is True
    assert calls == []


def test_human_policy_refuses_a_client_that_cannot_ask(resets, monkeypatch) -> None:
    calls, server = resets
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    result = _reset(server, {"confirm": True})
    assert result.is_error is True and "cannot ask" in result.content[0].text
    assert calls == []


def test_an_agent_cannot_confirm_an_irreversible_tool_by_default(resets, monkeypatch) -> None:
    calls, server = resets
    monkeypatch.delenv(confirm.POLICY_ENV, raising=False)
    text = _reset(server, {"confirm": True}).content[0].text
    assert calls == []
    assert "Stopped" in text and "cannot be undone" in text
    assert "confirm=True" not in text


def test_agent_policy_lets_an_unattended_bench_confirm(resets, monkeypatch) -> None:
    calls, server = resets
    monkeypatch.setenv(confirm.POLICY_ENV, "agent")
    assert _reset(server, {"confirm": True}).is_error is False
    assert len(calls) == 1


def test_human_policy_does_not_ask_for_a_reversible_tool(reboots, monkeypatch) -> None:
    calls, server = reboots
    assert confirm.policy({}) == "human"
    assert confirm.policy({confirm.POLICY_ENV: "HUMAN"}) == "human"
    with pytest.raises(ValueError, match=confirm.POLICY_ENV):
        confirm.policy({confirm.POLICY_ENV: "sometimes"})
    # `reboot` is reversible, so the human policy leaves it alone; only irreversible tools opt in.
    monkeypatch.setenv(confirm.POLICY_ENV, "human")
    handler, seen = _answer("accept")
    assert _call(server, {"confirm": True}, handler).is_error is False
    assert seen == [] and calls == [{"port": None, "confirm": True}]
