# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""meshtastic.protobuf resolves to meshtastic_mcp's generated protos, not the library's."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from meshtastic_mcp import protos


def test_protobuf_imports_resolve_to_the_generated_modules():
    from meshtastic.protobuf import mesh_pb2

    assert Path(mesh_pb2.__file__).parent == protos.GENERATED


def test_the_library_runs_on_the_generated_protos():
    import meshtastic.mesh_interface
    from meshtastic.protobuf import mesh_pb2

    assert meshtastic.mesh_interface.mesh_pb2 is mesh_pb2


def test_fields_newer_than_the_library_release_are_present():
    from meshtastic.protobuf import config_pb2, mesh_pb2, module_config_pb2

    assert "xeddsa_signed" in mesh_pb2.MeshPacket.DESCRIPTOR.fields_by_name
    assert "type" in mesh_pb2.ClientNotification.DESCRIPTOR.fields_by_name
    assert "ble_address" in mesh_pb2.DeviceMetadata.DESCRIPTOR.fields_by_name
    assert "packet_signature_policy" in config_pb2.Config.SecurityConfig.DESCRIPTOR.fields_by_name
    assert "mesh_beacon" in module_config_pb2.ModuleConfig.DESCRIPTOR.fields_by_name


def test_a_module_the_library_lacks_is_served():
    from meshtastic.protobuf import mesh_beacon_pb2

    assert Path(mesh_beacon_pb2.__file__).parent == protos.GENERATED


def test_the_protobufs_commit_is_recorded():
    assert re.fullmatch(r"[0-9a-f]{40}", protos.protobufs_ref())


def test_installing_again_keeps_one_finder():
    assert protos.install() is True
    finders = [f for f in sys.meta_path if isinstance(f, protos._GeneratedProtos)]
    assert len(finders) == 1


def test_stands_aside_when_the_library_protos_loaded_first(monkeypatch):
    monkeypatch.setattr(
        sys, "meta_path", [f for f in sys.meta_path if not isinstance(f, protos._GeneratedProtos)]
    )
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.fake_pb2", object())
    with pytest.warns(RuntimeWarning, match="was imported before meshtastic_mcp"):
        assert protos.install() is False
    assert not any(isinstance(f, protos._GeneratedProtos) for f in sys.meta_path)
