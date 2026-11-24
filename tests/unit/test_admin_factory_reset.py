# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""factory_reset must set the AdminMessage field the firmware dispatches on.

The firmware acts on *which* field is set: `factory_reset_device` wipes identity and BLE bonds,
`factory_reset_config` keeps them. meshtastic-python's `Node.factoryReset` sets the right one
from 2.7.9; these tests run that real method through the backend against a fake transport.
"""

from __future__ import annotations

import contextlib

import pytest
from meshtastic.node import Node

from meshtastic_mcp import connection
from meshtastic_mcp.backends import BackendError, legacy, python_admin


class _FakeNode:
    """Borrows the library's real factoryReset; records what it sends."""

    factoryReset = Node.factoryReset

    def __init__(self):
        self.sent = []
        self.iface = self

    @property
    def localNode(self):
        return self

    def ensureSessionKey(self):
        pass

    def _sendAdmin(self, msg, **kwargs):
        self.sent.append(msg)


class _FakeIface:
    def __init__(self):
        self.localNode = _FakeNode()


def _patch_connect(monkeypatch, iface):
    @contextlib.contextmanager
    def fake_connect(port=None, **kwargs):
        yield iface

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(python_admin.time, "sleep", lambda _s: None)


def test_full_reset_sets_device_field(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    legacy.factory_reset(confirm=True, full=True)
    msg = iface.localNode.sent[-1]
    assert msg.factory_reset_device == 1  # full wipe → the device field
    assert msg.factory_reset_config == 0


def test_config_only_reset_sets_config_field(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    legacy.factory_reset(confirm=True, full=False)
    msg = iface.localNode.sent[-1]
    assert msg.factory_reset_config == 1
    assert msg.factory_reset_device == 0


def test_factory_reset_requires_confirm(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    with pytest.raises(BackendError):
        legacy.factory_reset(confirm=False, full=True)
    assert iface.localNode.sent == []  # nothing sent without confirm
