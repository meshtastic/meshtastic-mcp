# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Guard the tool annotations.

`server._AnnotationTransform` applies each tool's `@meta` hints when tools are listed. The
guard here catches a tool with no declaration, and applied hints that disagree with the
declaration or the snapshot.

Runs in core-only mode (the portable unit tier), so it only asserts over
*registered* tools — the firmware-gated tools aren't present here.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module")
def server():
    # Deferred import: importing `server` autostarts the recorder, which
    # subscribes to the `meshtastic.log.line` pubsub topic. pypubsub locks a
    # topic's arg-spec to the first subscriber, so importing at module top
    # (collection time) would beat the root conftest's session fixture and
    # raise ListenerMismatchError for the whole suite. Importing here — after
    # session fixtures have subscribed — keeps the recorder a compatible
    # second subscriber. Annotations are applied at import regardless.
    from meshtastic_mcp import server as server_mod

    return server_mod


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.app.list_tools())}


def test_every_registered_tool_is_annotated(server):
    tools = _tools(server)
    assert tools, "no tools registered, so there is nothing to check"
    for name, tool in tools.items():
        assert tool.annotations is not None, (
            f"{name} has no annotations — the annotation pass may have failed open"
        )
        assert tool.annotations.title, f"{name} annotation missing a title"


def test_destructive_core_tools_carry_destructive_hint(server):
    tools = _tools(server)
    for name in ("reboot", "factory_reset", "set_config", "set_owner"):
        assert name in tools, f"{name} expected to be registered (core)"
        ann = tools[name].annotations
        assert ann.destructive_hint is True, f"{name} must be destructiveHint=True"
        assert ann.read_only_hint is False, f"{name} must not be readOnlyHint"


def test_read_only_core_tools_carry_read_only_hint(server):
    tools = _tools(server)
    for name in ("list_devices", "device_info", "list_nodes"):
        assert name in tools, f"{name} expected to be registered (core)"
        ann = tools[name].annotations
        assert ann.read_only_hint is True, f"{name} must be readOnlyHint=True"
        assert ann.destructive_hint is False, f"{name} must not be destructiveHint"


def _tool_functions() -> set[str]:
    """Every function under `tools/` registered with `@app.tool` or a capability decorator."""
    import ast
    from pathlib import Path

    from meshtastic_mcp import tools

    names = set()
    for path in Path(tools.__file__).parent.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                func = deco.func if isinstance(deco, ast.Call) else None
                if (isinstance(func, ast.Attribute) and func.attr == "tool") or (
                    isinstance(func, ast.Name) and func.id.endswith("_tool")
                ):
                    names.add(node.name)
    return names


def test_every_tool_declares_its_metadata(server):
    defined = _tool_functions()
    assert defined
    missing = defined - set(server.tool_meta.META)
    assert not missing, f"add @meta(...) above these tools: {sorted(missing)}"
    assert set(server.tool_meta.META) <= defined


def test_metadata_matches_the_snapshot(server):
    # Taken from the name-list maps the declarations replaced; a change here is deliberate.
    import json
    from pathlib import Path

    from meshtastic_mcp import toolsets

    snapshot = json.loads(
        (Path(__file__).parent / "data" / "tool_meta_snapshot.json").read_text(encoding="utf-8")
    )
    for name, want in snapshot.items():
        ann = server.tool_meta.hints(name)
        got = {
            "title": ann.title,
            "read_only": ann.read_only_hint,
            "destructive": ann.destructive_hint,
            "idempotent": ann.idempotent_hint,
            "open_world": ann.open_world_hint,
            "port": server.tool_meta.of(name).port,
            "toolset": toolsets.toolset_of(name),
        }
        assert got == want, name


def test_applied_annotations_match_the_declarations(server):
    for name, tool in _tools(server).items():
        assert tool.annotations == server.tool_meta.hints(name), name


def test_idempotent_is_only_for_device_writes(server):
    with pytest.raises(ValueError):
        server.tool_meta.meta("read", idempotent=True)


def test_set_config_is_idempotent(server):
    tools = _tools(server)
    assert "set_config" in tools
    ann = tools["set_config"].annotations
    assert ann.idempotent_hint is True, "set_config should be idempotentHint=True"
    assert ann.destructive_hint is True, "set_config must remain destructiveHint=True"


def test_tools_returning_mesh_content_are_open_world(server):
    # They return user-authored mesh content (untrusted input, lethal-trifecta leg 2).
    for name in ("logs_window", "packets_window", "query", "wait_for", "inspect"):
        assert server.tool_meta.of(name).open_world, name


def test_nrf52_bootloader_tools_are_open_world(server):
    # They reach a physical board through its bootloader drive, like the vendor flash tools.
    nrf52 = {name: tool for name, tool in _tools(server).items() if name.startswith("nrf52_")}
    assert nrf52
    for name, tool in nrf52.items():
        assert tool.annotations.open_world_hint is True, f"{name} must be openWorldHint=True"


def test_destructive_hint_survives_to_the_wire(server):
    """Annotations must reach the MCP protocol surface, not just in-process objects."""
    import asyncio

    from fastmcp import Client

    async def go():
        async with Client(server.app) as c:
            return {t.name: t.annotations for t in await c.list_tools()}

    ann = asyncio.run(go())
    assert ann["reboot"].destructive_hint is True
    assert ann["list_devices"].read_only_hint is True
