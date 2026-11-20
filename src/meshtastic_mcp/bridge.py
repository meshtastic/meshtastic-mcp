# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""One server per bench host, and the stdio bridge every other client uses to reach it.

`serve` holds an exclusive lock on `<data dir>/serve.lock` for its whole life and writes
where it listens to `<data dir>/serve.json`. A held lock is the proof a server is alive, so a
second `serve` on the host refuses to start and a server killed outright leaves nothing stale
behind. The default stdio entry finds that server and bridges to it instead of opening the
radios itself; `MESHTASTIC_MCP_STANDALONE=1` turns that off. Nothing here starts a server: its
capabilities and environment are fixed when it starts, and a server one session started would
carry that session's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

from . import toolsets

STANDALONE_ENV = "MESHTASTIC_MCP_STANDALONE"


class ServerAlreadyRunning(RuntimeError):
    """Another `serve` on this host holds the lock."""


def _data_dir() -> Path:
    from .config import mcp_data_dir

    d = mcp_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path() -> Path:
    return _data_dir() / "serve.lock"


def _info_path() -> Path:
    return _data_dir() / "serve.json"


def _try_lock(fh: IO[bytes]) -> bool:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def claim_host(url: str, token_file: str | None, version: str) -> IO[bytes]:
    """Take this host's server lock and publish where the server listens.

    Keep the returned handle open for the server's life; the lock goes with the process.
    Raises `ServerAlreadyRunning` naming the other server.
    """
    fh = open(_lock_path(), "a+b")  # noqa: SIM115  (held for the process lifetime)
    if not _try_lock(fh):
        fh.close()
        other = _read_info()
        where = other.get("url", "an unknown address") if other else "an unknown address"
        raise ServerAlreadyRunning(f"a meshtastic-mcp server already runs on this host at {where}")
    info = {"url": url, "token_file": token_file, "pid": os.getpid(), "version": version}
    tmp = _info_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, _info_path())
    return fh


def _read_info() -> dict[str, Any] | None:
    try:
        return json.loads(_info_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def find_local() -> dict[str, Any] | None:
    """The running server on this host, `{url, token_file, pid, version}`, or None."""
    if not _lock_path().exists():
        return None
    with open(_lock_path(), "a+b") as fh:
        if _try_lock(fh):
            return None  # nobody holds it: no server, whatever serve.json still says
    return _read_info()


def local_url(host: str, port: int) -> str:
    """The URL a client on this host uses for a server bound to `host`."""
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/mcp"


def auto_target(env: Mapping[str, str]) -> tuple[str, str] | None:
    """`(url, token)` of a local server the default stdio entry should bridge to, else None.

    Says on stderr why it does not bridge when a server is running.
    """
    if env.get(STANDALONE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    info = find_local()
    if not info:
        return None
    token_file = info.get("token_file")
    if not token_file:
        _note(
            f"a server runs at {info.get('url')} but took its token from the environment, so "
            "this client cannot read it; starting standalone, which races it for the radios"
        )
        return None
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        _note(f"cannot read the server's token file {token_file}: {exc}; starting standalone")
        return None
    return str(info["url"]), token


def _note(message: str) -> None:
    print(f"meshtastic-mcp: {message}", file=sys.stderr)


class ClientFilter(Transform):
    """Apply this client's `MESHTASTIC_MCP_TOOLSETS` / `_READ_ONLY` to the server's tools."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._toolsets = toolsets.parse_toolsets(env.get(toolsets.TOOLSETS_ENV))
        self._read_only = toolsets.read_only_requested(env)

    def _visible(self, tool: Tool) -> bool:
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        if self._read_only and not read_only:
            return False
        return self._toolsets is None or toolsets.toolset_of(tool.name) in self._toolsets

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t for t in tools if self._visible(t)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return tool if tool is not None and self._visible(tool) else None


def _server_version(url: str, headers: dict[str, str]) -> str:
    """Connect once; raises if the server does not answer."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def probe() -> str:
        async with Client(StreamableHttpTransport(url, headers=headers), timeout=10) as client:
            return str(getattr(client.server_info, "version", "") or "")

    return asyncio.run(probe())


def run(url: str, token: str, env: Mapping[str, str]) -> int:
    """Serve stdio here, forwarding every request to the server at `url`."""
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy

    from . import _version

    headers = {"Authorization": f"Bearer {token}"}
    try:
        theirs = _server_version(url, headers)
    except Exception as exc:
        _note(f"the server at {url} does not answer: {exc}")
        return 1
    ours = getattr(_version, "__version__", "")
    if theirs and ours and theirs != ours:
        _note(f"bridging to a {theirs} server from a {ours} client; the tools are the server's")
    proxy = create_proxy(StreamableHttpTransport(url, headers=headers), name="meshtastic")
    proxy.add_transform(ClientFilter(env))
    proxy.run(show_banner=False)
    return 0
