# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Every tool docstring is a description followed by at most an `Args:` section.

fastmcp makes the text before a docstring's first section the tool description and each `Args:`
entry that parameter's inputSchema `description`, and drops every other section (`Returns:`,
`Example:`, an admonition such as a line ending in a colon above an indented block) along with
anything after it. Claude Code shows the model the description and the inputSchema, not the
outputSchema, so a result shape belongs in the description and a parameter's prose in `Args:`.
Runs over every tool `tools/` defines, gated or not, so a host without a capability still checks
its tools.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def _tool_functions() -> dict[str, Callable[..., Any]]:
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server

    tools_dir = Path(server.__file__).parent / "tools"
    found: dict[str, Callable[..., Any]] = {}
    for source in sorted(tools_dir.glob("*.py")):
        module = importlib.import_module(f"meshtastic_mcp.tools.{source.stem}")
        for node in ast.parse(source.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _is_tool_decorator(d) for d in node.decorator_list
            ):
                found[node.name] = getattr(module, node.name)
    return found


def _is_tool_decorator(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "tool" and isinstance(func.value, ast.Name) and func.value.id == "app"
    return isinstance(func, ast.Name) and func.id.endswith("_tool")


@pytest.fixture(scope="module")
def tool_functions() -> dict[str, Callable[..., Any]]:
    return _tool_functions()


def _sections(fn: Callable[..., Any]) -> list[str]:
    from griffe import Docstring

    doc = inspect.getdoc(fn) or ""
    return [s.kind.value for s in Docstring(doc, lineno=1, parser="google").parse()]


def test_tool_discovery_finds_gated_tools(tool_functions) -> None:
    assert {"device_info", "build", "android_tap", "rf_scan", "discord_search"} <= set(
        tool_functions
    )


def test_docstrings_hold_only_a_description_and_args(tool_functions) -> None:
    bad = {}
    for name, fn in tool_functions.items():
        kinds = _sections(fn)
        if kinds not in (["text"], ["text", "parameters"]):
            bad[name] = kinds
    assert bad == {}, "sections fastmcp drops, or text after a section; see the module docstring"


def test_description_is_the_whole_text_before_args(tool_functions) -> None:
    from fastmcp.tools import Tool

    lost = {}
    for name, fn in tool_functions.items():
        doc = inspect.getdoc(fn) or ""
        expected = doc.split("\n\nArgs:\n", 1)[0].rstrip()
        description = Tool.from_function(fn).description
        if not expected or description != expected:
            lost[name] = description
    assert lost == {}


def test_every_parameter_has_a_description(tool_functions) -> None:
    from fastmcp.tools import Tool

    missing = {}
    for name, fn in tool_functions.items():
        properties = Tool.from_function(fn).parameters.get("properties", {})
        empty = sorted(p for p, schema in properties.items() if not schema.get("description"))
        if empty:
            missing[name] = empty
    assert missing == {}, "document each in the tool's Args: section"


def test_args_name_only_real_parameters(tool_functions) -> None:
    from fastmcp.utilities.docstring_parsing import parse_docstring

    stale = {}
    for name, fn in tool_functions.items():
        params = set(inspect.signature(fn).parameters) - {"ctx"}
        extra = set(parse_docstring(fn).parameters) - params
        if extra:
            stale[name] = sorted(extra)
    assert stale == {}
