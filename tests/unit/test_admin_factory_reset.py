# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""factory_reset must set the CORRECT AdminMessage field.

The firmware dispatches on *which* field is set, not its int value. A prior bug
always set `factory_reset_config` (with 1 vs 2), so `full=True` silently did a
config-only reset — BLE bonds + identity key survived a requested full wipe.
These hardware-free tests assert the right field is selected for each mode.
"""

from __future__ import annotations

import contextlib

import pytest

from meshtastic_mcp import admin


class _FakeNode:
    def __init__(self):
        self.sent = []

    def _sendAdmin(self, msg):
        self.sent.append(msg)


class _FakeIface:
    def __init__(self):
        self.localNode = _FakeNode()


def _patch_connect(monkeypatch, iface):
    @contextlib.contextmanager
    def fake_connect(port=None, **kwargs):
        yield iface

    monkeypatch.setattr(admin, "connect", fake_connect)


def test_full_reset_sets_device_field(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    admin.factory_reset(confirm=True, full=True)
    msg = iface.localNode.sent[-1]
    assert msg.factory_reset_device == 1  # full wipe → the device field
    assert msg.factory_reset_config == 0


def test_config_only_reset_sets_config_field(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    admin.factory_reset(confirm=True, full=False)
    msg = iface.localNode.sent[-1]
    assert msg.factory_reset_config == 1
    assert msg.factory_reset_device == 0


def test_factory_reset_requires_confirm(monkeypatch):
    iface = _FakeIface()
    _patch_connect(monkeypatch, iface)
    with pytest.raises(admin.AdminError):
        admin.factory_reset(confirm=False, full=True)
    assert iface.localNode.sent == []  # nothing sent without confirm
