# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Each tool's hints and port handling, declared with `@meta(...)` above its definition.

`kind` is `read` (observes only), `write` (changes host state another call undoes, no device)
or `destructive` (changes a device or can't be undone; most are confirm-gated too).
`open_world` marks a tool that reaches outside hardware or returns content a remote node
authored. `port` says what a `port`/`node` argument means for leases: `device` by default,
`not_device` for a listen port, hub port or filter, `finds_own` for a tool that picks a board
when none is named.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from mcp.types import ToolAnnotations

Kind = Literal["read", "write", "destructive"]
PortMode = Literal["device", "not_device", "finds_own"]

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ToolMeta:
    kind: Kind
    open_world: bool = False
    idempotent: bool = False
    title: str | None = None
    port: PortMode = "device"


META: dict[str, ToolMeta] = {}


def meta(
    kind: Kind,
    *,
    open_world: bool = False,
    idempotent: bool = False,
    title: str | None = None,
    port: PortMode = "device",
) -> Callable[[F], F]:
    """Record the tool's metadata under its function name; goes directly above `def`."""
    if idempotent and kind != "destructive":
        raise ValueError("idempotent is implied for reads and only meaningful for device writes")

    def deco(fn: F) -> F:
        name = fn.__name__
        if name in META:
            raise ValueError(f"tool metadata for {name!r} declared twice")
        META[name] = ToolMeta(kind, open_world, idempotent, title, port)
        return fn

    return deco


def of(name: str) -> ToolMeta:
    try:
        return META[name]
    except KeyError:
        raise KeyError(f"tool {name!r} has no @meta(...) declaration") from None


def hints(name: str) -> ToolAnnotations:
    m = of(name)
    read_only = m.kind == "read"
    return ToolAnnotations(
        title=m.title or name.replace("_", " ").title(),
        read_only_hint=read_only,
        destructive_hint=m.kind == "destructive",
        idempotent_hint=read_only or m.idempotent,
        open_world_hint=m.open_world,
    )


def read_only_names() -> set[str]:
    return {name for name, m in META.items() if m.kind == "read"}
