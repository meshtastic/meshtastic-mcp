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


def test_android_docs_tools_registered_and_readonly(server) -> None:
    # android_docs_* tools are capability-gated: they only register when the
    # android capability is active (android CLI + adb present). In CI and on
    # machines without android tooling they are intentionally absent.
    tools = server.app._tool_manager._tools
    if not server.CAPS.android:
        pytest.skip("android capability inactive — android_docs tools not registered")
    for name in ("android_docs_search", "android_docs_fetch"):
        assert name in tools, f"{name} not registered"
        ann = tools[name].annotations
        assert ann is not None and ann.readOnlyHint and ann.openWorldHint


def test_resources_registered(server) -> None:
    rm = server.app._resource_manager
    assert "meshtastic://doctor" in rm._resources
    assert "meshtastic://capabilities" in rm._resources
    assert "meshtastic://e2e/{loop}" in rm._templates


def test_prompts_registered(server) -> None:
    names = set(server.app._prompt_manager._prompts)
    assert {"triage_e2e_failure", "bringup_device", "inbound_loop"} <= names


def test_e2e_resource_serves_bundled_docs_and_rejects_traversal(server) -> None:
    assert "# Loop: inbound" in server._resource_e2e_loop("loop-inbound")
    # path-traversal / unknown returns the available list, never file contents outside refs
    out = server._resource_e2e_loop("../../../../etc/passwd")
    assert "unknown loop" in out and "root:" not in out
