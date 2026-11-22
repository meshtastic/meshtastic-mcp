# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Dot-paths into a radio's config protobufs, with no radio I/O.

The first segment names a section (`lora` in LocalConfig, `mqtt` in LocalModuleConfig); the
rest walk protobuf fields. Enum fields accept their names (`"US"` for `lora.region`).
"""

from __future__ import annotations

import inspect
from typing import Any

from google.protobuf import descriptor as pb_descriptor
from google.protobuf import json_format
from meshtastic.protobuf import localonly_pb2

from . import BackendError

LOCAL_CONFIG_SECTIONS = {f.name for f in localonly_pb2.LocalConfig.DESCRIPTOR.fields}
MODULE_CONFIG_SECTIONS = {f.name for f in localonly_pb2.LocalModuleConfig.DESCRIPTOR.fields}


class PathError(BackendError):
    """A config path or value the protos do not accept."""


def message_to_dict(msg: Any) -> dict[str, Any]:
    """A message as a dict with proto field names, unset fields left out, secrets kept."""
    kwargs: dict[str, Any] = {"preserving_proto_field_name": True}
    params = inspect.signature(json_format.MessageToDict).parameters
    # protobuf 5.26 renamed `including_default_value_fields`.
    if "always_print_fields_with_no_presence" in params:
        kwargs["always_print_fields_with_no_presence"] = False
    elif "including_default_value_fields" in params:
        kwargs["including_default_value_fields"] = False
    return json_format.MessageToDict(msg, **kwargs)


def section_container(node: Any, section: str) -> tuple[Any, str]:
    """The section's message on `node` and its parent, `localConfig` or `moduleConfig`."""
    if section in LOCAL_CONFIG_SECTIONS:
        return getattr(node.localConfig, section), "localConfig"
    if section in MODULE_CONFIG_SECTIONS:
        return getattr(node.moduleConfig, section), "moduleConfig"
    raise PathError(
        f"Unknown config section: {section!r}. "
        f"Valid sections: {sorted(LOCAL_CONFIG_SECTIONS | MODULE_CONFIG_SECTIONS)}"
    )


def _coerce_enum(field: pb_descriptor.FieldDescriptor, value: Any) -> int:
    enum_type = field.enum_type
    if isinstance(value, bool):
        raise PathError(f"{field.name}: expected enum {enum_type.name}, got bool")
    if isinstance(value, int):
        if enum_type.values_by_number.get(value) is None:
            raise PathError(f"{field.name}: {value} is not a valid {enum_type.name} value")
        return value
    if isinstance(value, str):
        ev = enum_type.values_by_name.get(value.upper())
        if ev is None:
            valid = sorted(enum_type.values_by_name.keys())
            raise PathError(
                f"{field.name}: {value!r} is not a valid {enum_type.name}. Valid: {valid}"
            )
        return ev.number
    raise PathError(f"{field.name}: expected enum {enum_type.name}, got {type(value).__name__}")


def coerce_scalar(field: pb_descriptor.FieldDescriptor, value: Any) -> Any:
    """`value` as the field's type; enum names and "true"/"false" strings are accepted."""
    t = field.type
    FT = pb_descriptor.FieldDescriptor
    if t == FT.TYPE_ENUM:
        return _coerce_enum(field, value)
    if t == FT.TYPE_BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1", "on")
        if isinstance(value, int):
            return bool(value)
    if t in (
        FT.TYPE_INT32,
        FT.TYPE_INT64,
        FT.TYPE_UINT32,
        FT.TYPE_UINT64,
        FT.TYPE_SINT32,
        FT.TYPE_SINT64,
        FT.TYPE_FIXED32,
        FT.TYPE_FIXED64,
    ):
        return int(value)
    if t in (FT.TYPE_FLOAT, FT.TYPE_DOUBLE):
        return float(value)
    if t == FT.TYPE_STRING:
        return str(value)
    if t == FT.TYPE_BYTES:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return str(value).encode("utf-8")
    raise PathError(f"{field.name}: unsupported field type {t}")


def walk_to_field(
    root_msg: Any, path_segments: list[str]
) -> tuple[Any, pb_descriptor.FieldDescriptor]:
    """Walk `root_msg` by field names; return (parent message, leaf field descriptor)."""
    msg = root_msg
    for i, name in enumerate(path_segments):
        desc = msg.DESCRIPTOR
        field = desc.fields_by_name.get(name)
        if field is None:
            trail = ".".join(path_segments[:i] or ["<root>"])
            raise PathError(f"No field {name!r} in {trail}. Valid: {[f.name for f in desc.fields]}")
        if i == len(path_segments) - 1:
            return msg, field
        if field.type != pb_descriptor.FieldDescriptor.TYPE_MESSAGE:
            raise PathError(
                f"{'.'.join(path_segments[: i + 1])} is a scalar; cannot descend into it"
            )
        msg = getattr(msg, name)
    raise PathError("Empty config path")
