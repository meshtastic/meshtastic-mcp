# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A meshtastic-python refusal inside `connect` comes back as a ToolError."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node

from meshtastic_mcp import admin, connection


@pytest.fixture
def radio(monkeypatch: pytest.MonkeyPatch) -> Iterator[MeshInterface]:
    iface = MeshInterface(noProto=True)
    iface.localNode = Node(iface, 0x1234, noProto=True)
    iface.localNode.channels = []

    @contextlib.contextmanager
    def fake_connect(port: Any, timeout_s: float, linger_s: float) -> Iterator[MeshInterface]:
        yield iface

    monkeypatch.setattr(connection, "_connect", fake_connect)
    yield iface
    iface.close()


def test_a_url_the_library_rejects_is_a_tool_error(radio: MeshInterface) -> None:
    real_stdout = sys.stdout
    with pytest.raises(ToolError, match=r"refused the request: .*Invalid URL"):
        admin.set_channel_url("https://meshtastic.org/e/LongFast")
    assert sys.stdout is real_stdout


def test_other_errors_pass_through_unchanged(radio: MeshInterface) -> None:
    with pytest.raises(ValueError, match="boom"), connection.connect("x"):
        raise ValueError("boom")
