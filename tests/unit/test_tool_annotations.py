# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Guard the post-registration tool-annotation pass.

`server._apply_tool_annotations()` reaches into FastMCP's private
`app._tool_manager._tools` inside a broad try/except. If that private path is
ever renamed, the except would swallow it and EVERY `destructiveHint` would
silently vanish — defeating client-side gating, which violates the project rule.
These assertions fail loudly in that case and also catch annotation-set drift
(a tool whose applied hints disagree with the classification maps).

Runs in core-only mode (the portable unit tier), so it only asserts over
*registered* tools — the firmware-gated tools aren't present here.
"""

from __future__ import annotations

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
    return server.app._tool_manager._tools


def test_every_registered_tool_is_annotated(server):
    tools = _tools(server)
    assert tools, "no tools registered — _apply_tool_annotations precondition broken"
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
        assert ann.destructiveHint is True, f"{name} must be destructiveHint=True"
        assert ann.readOnlyHint is False, f"{name} must not be readOnlyHint"


def test_read_only_core_tools_carry_read_only_hint(server):
    tools = _tools(server)
    for name in ("list_devices", "device_info", "list_nodes"):
        assert name in tools, f"{name} expected to be registered (core)"
        ann = tools[name].annotations
        assert ann.readOnlyHint is True, f"{name} must be readOnlyHint=True"
        assert ann.destructiveHint is False, f"{name} must not be destructiveHint"


def test_annotation_sets_have_no_contradiction(server):
    # A tool cannot be both read-only and destructive.
    overlap = server._READ_ONLY & server._DESTRUCTIVE
    assert not overlap, f"Tools in both _READ_ONLY and _DESTRUCTIVE: {overlap}"


def test_idempotent_writes_are_subset_of_destructive(server):
    # Idempotent writes must also be classified as destructive (they mutate state).
    not_destructive = server._IDEMPOTENT_WRITES - server._DESTRUCTIVE
    assert not not_destructive, (
        f"_IDEMPOTENT_WRITES entries missing from _DESTRUCTIVE: {not_destructive}"
    )


def test_applied_annotations_match_classification_maps(server):
    # Every registered tool's applied hints agree with the maps — catches both
    # the fail-open (annotations present but wrong) and map/registry drift.
    for name, tool in _tools(server).items():
        ann = tool.annotations
        assert ann.readOnlyHint == (name in server._READ_ONLY), f"{name}: readOnlyHint mismatch"
        assert ann.destructiveHint == (name in server._DESTRUCTIVE), (
            f"{name}: destructiveHint mismatch"
        )
        assert ann.openWorldHint == (name in server._OPEN_WORLD), f"{name}: openWorldHint mismatch"
        expected_idempotent = (name in server._READ_ONLY) or (name in server._IDEMPOTENT_WRITES)
        assert ann.idempotentHint == expected_idempotent, (
            f"{name}: idempotentHint mismatch (expected {expected_idempotent})"
        )


def test_no_unannotated_tools(server):
    # Every tool must be in at least _READ_ONLY or _DESTRUCTIVE.
    # Tools in neither fall through to worst-case defaults (destructive,
    # non-idempotent, open-world) which is wrong for most of them.
    all_classified = server._READ_ONLY | server._DESTRUCTIVE
    for name in _tools(server):
        assert name in all_classified, (
            f"{name} is not in _READ_ONLY or _DESTRUCTIVE — add it to the correct set in server.py"
        )


def test_set_config_is_idempotent(server):
    tools = _tools(server)
    assert "set_config" in tools
    ann = tools["set_config"].annotations
    assert ann.idempotentHint is True, "set_config should be idempotentHint=True"
    assert ann.destructiveHint is True, "set_config must remain destructiveHint=True"


def test_logs_and_packets_window_are_open_world(server):
    # These return user-authored mesh content (untrusted input, lethal-trifecta leg 2).
    for name in ("logs_window", "packets_window"):
        assert name in server._OPEN_WORLD, (
            f"{name} must be in _OPEN_WORLD (returns untrusted mesh content)"
        )


def test_title_overrides_produce_correct_titles(server):
    tools = _tools(server)
    for tool_name, expected_title in server._TITLE_OVERRIDES.items():
        if tool_name not in tools:
            continue  # gated tool not registered in this capability tier
        ann = tools[tool_name].annotations
        assert ann.title == expected_title, (
            f"{tool_name}: title {ann.title!r} != {expected_title!r}"
        )
