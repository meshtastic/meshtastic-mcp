# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Protobuf messages as dicts with every field present and sensitive values withheld."""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message
from meshtastic.protobuf import field_metadata_pb2

REDACTED = "<redacted>"

_FIELD_METADATA = field_metadata_pb2.field_metadata


def _sensitive(field: FieldDescriptor) -> bool:
    options = field.GetOptions()
    return options.HasExtension(_FIELD_METADATA) and options.Extensions[_FIELD_METADATA].sensitive


def _redact(data: dict[str, Any], descriptor: Descriptor) -> dict[str, Any]:
    for field in descriptor.fields:
        if field.name not in data:
            continue
        value = data[field.name]
        if _sensitive(field):
            # An empty value stays, so "no password set" is still visible.
            if value not in ("", 0, [], None):
                data[field.name] = REDACTED
        elif field.message_type is not None and isinstance(value, dict):
            _redact(value, field.message_type)
        elif field.message_type is not None and isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _redact(item, field.message_type)
    return data


def to_dict(msg: Message) -> dict[str, Any]:
    """`msg` with default-valued fields present, enums by name and sensitive fields withheld."""
    data = json_format.MessageToDict(
        msg,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    return _redact(data, msg.DESCRIPTOR)


def sensitive_paths(descriptor: Descriptor, prefix: str) -> frozenset[str]:
    """Dot-paths under `prefix` of every field the protos mark sensitive."""
    paths: set[str] = set()
    for field in descriptor.fields:
        path = f"{prefix}.{field.name}"
        if _sensitive(field):
            paths.add(path)
        elif field.message_type is not None and not field.is_repeated:
            paths |= sensitive_paths(field.message_type, path)
    return frozenset(paths)
