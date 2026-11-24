# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`app_view` and `app` reach the tool behind each platform/action, and refuse what has none."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from meshtastic_mcp.tools import android, app_drive, apple


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _capable(monkeypatch) -> None:
    monkeypatch.setattr(app_drive, "_capable", lambda platform: True)
    monkeypatch.delenv(app_drive.DESKTOP_URL_ENV, raising=False)
    monkeypatch.delenv(app_drive.DESKTOP_PORT_ENV, raising=False)


def _spy(monkeypatch, module: Any, name: str, result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(*args: Any, **kwargs: Any) -> Any:
        assert not args
        calls.append(kwargs)
        return result

    monkeypatch.setattr(module, name, fake)
    return calls


# (platform, action, args given to the tool, backing module, backing name, kwargs it receives)
_VIEW_CASES = [
    ("android", "screenshot", {"serial": "s1", "annotate": True}, android, "android_screenshot"),
    ("android", "screenshot", {}, android, "android_screenshot"),
    ("android", "ui_dump", {"diff": False}, android, "android_ui_dump"),
    ("android", "find_text", {"token": "Hi", "serial": "s1"}, android, "android_find_text"),
    (
        "android",
        "poll_for_text",
        {"token": "Hi", "timeout": 5.0, "interval": 0.5},
        android,
        "android_poll_for_text",
    ),
    (
        "android",
        "read_logcat",
        {"tags": ["MNGATT"], "grep": "x|y"},
        android,
        "android_read_logcat",
    ),
    ("android", "resolve", {"label": 3}, android, "android_resolve"),
    ("ios", "screenshot", {"udid": "U"}, apple, "ios_screenshot"),
    ("ios", "ui_dump", {}, apple, "ios_ui_dump"),
]

_CHANGE_CASES = [
    ("android", "tap", {"x": 1, "y": 2}, android, "android_tap"),
    ("android", "tap", {"label": 4, "serial": "s1"}, android, "android_tap"),
    (
        "android",
        "swipe",
        {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 100},
        android,
        "android_swipe",
    ),
    ("android", "type_text", {"text": "hello world"}, android, "android_type_text"),
    ("android", "clear_logcat", {"serial": "s1"}, android, "android_clear_logcat"),
    ("ios", "boot", {"name_contains": "iPad"}, apple, "ios_boot"),
    ("ios", "open_url", {"port": 4403, "udid": "U"}, apple, "ios_open_url"),
    ("ios", "tap", {"x": 5, "y": 6}, apple, "ios_tap"),
    ("ios", "type_text", {"text": "hi", "udid": "U"}, apple, "ios_type_text"),
]


@pytest.mark.parametrize(("platform", "action", "args", "module", "name"), _VIEW_CASES)
def test_a_view_action_reaches_its_backing_tool(
    monkeypatch, platform, action, args, module, name
) -> None:
    calls = _spy(monkeypatch, module, name, {"sentinel": name})
    out = _run(app_drive.app_view(platform, action, **args))
    assert calls == [args]
    assert out == {"platform": platform, "action": action, "result": {"sentinel": name}}


@pytest.mark.parametrize(("platform", "action", "args", "module", "name"), _CHANGE_CASES)
def test_a_change_action_reaches_its_backing_tool(
    monkeypatch, platform, action, args, module, name
) -> None:
    calls = _spy(monkeypatch, module, name, {"ok": True})
    out = _run(app_drive.app(platform, action, **args))
    assert calls == [args]
    assert out == {"platform": platform, "action": action, "result": {"ok": True}}


def test_a_non_dict_result_comes_back_whole(monkeypatch) -> None:
    _spy(monkeypatch, android, "android_find_text", True)
    out = _run(app_drive.app_view("android", "find_text", token="x"))
    assert out["result"] is True


def test_a_parameter_the_action_does_not_take_is_refused_with_the_right_ones() -> None:
    with pytest.raises(ToolError, match=r"does not take \['udid'\].*'token', 'serial'"):
        _run(app_drive.app_view("android", "find_text", token="x", udid="U"))
    with pytest.raises(ToolError, match=r"does not take \['y'\]"):
        _run(app_drive.app("ios", "type_text", text="x", y=1))
    with pytest.raises(ToolError, match="no parameters"):
        _run(app_drive.app_view("desktop", "connection_state", limit=3))


def test_a_missing_required_parameter_is_refused() -> None:
    with pytest.raises(ToolError, match=r"needs \['token'\]"):
        _run(app_drive.app_view("android", "find_text"))
    with pytest.raises(ToolError, match=r"needs \['x2', 'y2'\]"):
        _run(app_drive.app("android", "swipe", x1=1, y1=2))


def test_a_read_that_would_change_the_simulator_is_sent_to_ios_screenshot() -> None:
    with pytest.raises(ToolError, match="ios_screenshot"):
        _run(app_drive.app_view("ios", "screenshot", appearance="dark"))
    with pytest.raises(ToolError, match="does not take"):
        _run(app_drive.app_view("android", "screenshot", clean_status_bar=True))


def test_a_pair_with_no_backing_tool_names_what_the_platform_supports() -> None:
    with pytest.raises(ToolError, match=r"`find_text` on ios.*'screenshot', 'ui_dump'"):
        _run(app_drive.app_view("ios", "find_text", token="x"))
    with pytest.raises(ToolError, match=r"`connect` on android.*'clear_logcat'.*'tap'"):
        _run(app_drive.app("android", "connect", address="x"))
    with pytest.raises(ToolError, match=r"`screenshot` on desktop.*'connection_state', 'nodes'"):
        _run(app_drive.app_view("desktop", "screenshot"))


def test_a_read_asked_of_app_and_a_change_asked_of_app_view_name_the_other_tool() -> None:
    with pytest.raises(ToolError, match="`app_view` action"):
        _run(app_drive.app("android", "ui_dump"))
    with pytest.raises(ToolError, match="`app` action"):
        _run(app_drive.app_view("android", "tap", serial="s1"))


def test_an_inactive_capability_points_at_doctor(monkeypatch) -> None:
    monkeypatch.setattr(app_drive, "_capable", lambda platform: False)
    with pytest.raises(ToolError, match="doctor"):
        _run(app_drive.app_view("ios", "ui_dump"))


# ---- desktop: a stand-in for the desktop app's loopback MCP server ----------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def desktop_server() -> Iterator[tuple[str, list[tuple[str, dict[str, Any]]]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    srv = FastMCP("meshtastic-desktop")

    @srv.tool()
    def connection_state() -> str:
        seen.append(("connection_state", {}))
        return '{"state":"Connected","node_count":2}'

    @srv.tool()
    def nodes(limit: int = 50) -> str:
        seen.append(("nodes", {"limit": limit}))
        return '{"total":2,"nodes":[]}'

    @srv.tool()
    def connect(address: str, wait_seconds: int = 30) -> str:
        seen.append(("connect", {"address": address, "wait_seconds": wait_seconds}))
        return '{"connected":true}'

    @srv.tool()
    def send_text(text: str, to: str | None = None) -> str:
        seen.append(("send_text", {"text": text, "to": to}))
        raise ValueError("not connected")

    port = _free_port()
    thread = threading.Thread(
        target=lambda: srv.run(
            transport="http", host="127.0.0.1", port=port, show_banner=False, log_level="error"
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail("the stand-in desktop server did not start")
    yield f"http://127.0.0.1:{port}/mcp", seen


def test_desktop_actions_reach_the_desktop_server(monkeypatch, desktop_server) -> None:
    url, seen = desktop_server
    monkeypatch.setenv(app_drive.DESKTOP_URL_ENV, url)
    state = _run(app_drive.app_view("desktop", "connection_state"))
    assert state == {
        "platform": "desktop",
        "action": "connection_state",
        "result": {"state": "Connected", "node_count": 2},
    }
    assert _run(app_drive.app_view("desktop", "nodes", limit=5))["result"]["total"] == 2
    out = _run(app_drive.app("desktop", "connect", address="28:84:85:78:4E:ED"))
    assert out["result"] == {"connected": True}
    assert seen == [
        ("connection_state", {}),
        ("nodes", {"limit": 5}),
        ("connect", {"address": "28:84:85:78:4E:ED", "wait_seconds": 30}),
    ]


def test_a_desktop_tool_error_comes_back_as_a_tool_error(monkeypatch, desktop_server) -> None:
    url, _ = desktop_server
    monkeypatch.setenv(app_drive.DESKTOP_URL_ENV, url)
    with pytest.raises(ToolError, match="desktop send_text"):
        _run(app_drive.app("desktop", "send_text", text="hi"))


def test_the_desktop_url_falls_back_to_the_apps_port_variable(monkeypatch) -> None:
    monkeypatch.setenv(app_drive.DESKTOP_PORT_ENV, "8123")
    assert app_drive.desktop_url() == "http://127.0.0.1:8123/mcp"
    monkeypatch.setenv(app_drive.DESKTOP_URL_ENV, "http://127.0.0.1:9/mcp")
    assert app_drive.desktop_url() == "http://127.0.0.1:9/mcp"


def test_no_desktop_address_says_how_to_set_one() -> None:
    with pytest.raises(ToolError, match=app_drive.DESKTOP_URL_ENV):
        _run(app_drive.app_view("desktop", "connection_state"))


def test_a_desktop_app_that_is_not_running_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.setenv(app_drive.DESKTOP_URL_ENV, f"http://127.0.0.1:{_free_port()}/mcp")
    with pytest.raises(ToolError, match=r"does not answer.*debug build"):
        _run(app_drive.app_view("desktop", "connection_state"))


def test_the_two_tools_keep_true_hints() -> None:
    from meshtastic_mcp import server

    tools = {t.name: t for t in asyncio.run(server.app.list_tools())}
    view, change = tools["app_view"].annotations, tools["app"].annotations
    assert view.read_only_hint and view.open_world_hint
    assert not change.read_only_hint and change.destructive_hint and change.open_world_hint
    assert len(tools["app"].description) < 4096 and len(tools["app_view"].description) < 4096
