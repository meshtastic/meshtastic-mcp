# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Guards for the MCP surface: new tools, resources, and prompts stay registered + annotated."""

from __future__ import annotations

import pytest


@pytest.fixture
def server():
    # Deferred import: importing `server` autostarts the recorder, which binds a pubsub
    # topic's arg-spec to the first subscriber — a module-top import races the session
    # log-mirror fixture. Import inside the fixture instead (matches test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


def _registered_tools(app) -> dict[str, object]:
    return {
        tool.name: tool
        for key, tool in app.local_provider._components.items()
        if key.startswith("tool:")
    }


def _component_uris(app, kind: str) -> set[str]:
    # Keys are "{kind}:{identifier}@{version}" (version may be empty → trailing "@").
    # Strip only the final "@version" delimiter so identifiers containing "@" stay intact.
    prefix = f"{kind}:"
    return {
        entry[len(prefix) :].rsplit("@", 1)[0] if "@" in entry else entry
        for entry in (str(k) for k in app.local_provider._components)
        if entry.startswith(prefix)
    }


def test_android_docs_tools_registered_and_readonly(server) -> None:
    # android_docs_* tools are capability-gated: they only register when the
    # android capability is active (android CLI + adb present). In CI and on
    # machines without android tooling they are intentionally absent.
    tools = _registered_tools(server.app)
    if not server.CAPS.android:
        pytest.skip("android capability inactive — android_docs tools not registered")
    for name in ("android_docs_search", "android_docs_fetch"):
        assert name in tools, f"{name} not registered"
        ann = tools[name].annotations
        assert ann is not None and ann.readOnlyHint and ann.openWorldHint


def test_android_driving_tools_registered(server) -> None:
    tools = _registered_tools(server.app)
    if not server.CAPS.android:
        pytest.skip("android capability inactive — driving tools not registered")
    expected_read_only = {
        "android_ui_dump",
        "android_resolve",
        "android_find_text",
        "android_poll_for_text",
        "android_read_logcat",
    }
    expected_destructive = {
        "android_screenshot",
        "android_tap",
        "android_swipe",
        "android_type_text",
        "android_clear_logcat",
    }
    for name in expected_read_only | expected_destructive:
        assert name in tools, f"{name} not registered"
    for name in expected_read_only:
        assert tools[name].annotations.readOnlyHint, f"{name} should be read-only"
    for name in expected_destructive:
        assert tools[name].annotations.destructiveHint, f"{name} should be destructive"


def test_android_type_text_encodes_spaces(server, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "meshtastic_mcp.emulator.avd.type_text",
        lambda text, serial=None: calls.append(text),
    )
    server.android_type_text("hello world", serial="emulator-5554")
    assert calls == ["hello%sworld"]


def test_android_poll_for_text_rejects_bad_timeout_and_interval(server) -> None:
    with pytest.raises(ValueError, match="timeout"):
        server.android_poll_for_text("token", timeout=-1)
    with pytest.raises(ValueError, match="timeout"):
        server.android_poll_for_text("token", timeout=float("inf"))
    with pytest.raises(ValueError, match="interval"):
        server.android_poll_for_text("token", interval=0)
    with pytest.raises(ValueError, match="interval"):
        server.android_poll_for_text("token", interval=-0.5)


def test_resources_registered(server) -> None:
    resources = _component_uris(server.app, "resource")
    templates = _component_uris(server.app, "template")
    assert "meshtastic://doctor" in resources
    assert "meshtastic://capabilities" in resources
    assert "meshtastic://e2e/{loop}" in templates


def test_prompts_registered(server) -> None:
    names = _component_uris(server.app, "prompt")
    assert {"triage_e2e_failure", "bringup_device", "inbound_loop"} <= names


def test_e2e_resource_serves_bundled_docs_and_rejects_traversal(server) -> None:
    assert "# Loop: inbound" in server._resource_e2e_loop("loop-inbound")
    # path-traversal / unknown returns the available list, never file contents outside refs
    out = server._resource_e2e_loop("../../../../etc/passwd")
    assert "unknown loop" in out and "root:" not in out
