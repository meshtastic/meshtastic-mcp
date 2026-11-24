# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""One server per host, its discovery, and the stdio bridge to it."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshtastic_mcp import bridge

TOKEN = "t" * 40


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


_HOLD = """
import sys, time
from meshtastic_mcp import bridge
held = bridge.claim_host(sys.argv[1], sys.argv[2], "9.9.9")
print("held", flush=True)
time.sleep(600)
"""


def _hold_in_child(data_dir: Path, url: str, token_file: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, url, token_file],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "MESHTASTIC_MCP_DATA_DIR": str(data_dir)},
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def test_a_live_server_is_found_and_a_second_one_refused(data_dir: Path) -> None:
    proc = _hold_in_child(data_dir, "http://127.0.0.1:9/mcp", "/tok")
    try:
        assert bridge.find_local() == {
            "url": "http://127.0.0.1:9/mcp",
            "token_file": "/tok",
            "pid": proc.pid,
            "version": "9.9.9",
        }
        with pytest.raises(bridge.ServerAlreadyRunning, match=r"127\.0\.0\.1:9"):
            bridge.claim_host("http://127.0.0.1:10/mcp", None, "1")
    finally:
        proc.kill()
        proc.wait()


def test_a_server_killed_outright_leaves_nothing_to_find(data_dir: Path) -> None:
    proc = _hold_in_child(data_dir, "http://127.0.0.1:9/mcp", "/tok")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    assert (data_dir / "serve.json").exists()
    assert bridge.find_local() is None
    bridge.claim_host("http://127.0.0.1:10/mcp", None, "1").close()


def test_no_server_means_no_bridge(data_dir: Path) -> None:
    assert bridge.find_local() is None
    assert bridge.auto_target({}) is None


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8765/mcp"),
        ("192.168.1.168", "http://192.168.1.168:8765/mcp"),
        ("::", "http://[::1]:8765/mcp"),
    ],
)
def test_local_url(host: str, url: str) -> None:
    assert bridge.local_url(host, 8765) == url


def test_auto_target_reads_the_token_file_and_honors_standalone(data_dir: Path, capsys) -> None:
    token_file = data_dir / "token"
    token_file.write_text(TOKEN)
    held = bridge.claim_host("http://127.0.0.1:9/mcp", str(token_file), "1")
    try:
        assert bridge.auto_target({}) == ("http://127.0.0.1:9/mcp", TOKEN)
        assert bridge.auto_target({bridge.STANDALONE_ENV: "1"}) is None
    finally:
        held.close()


def test_a_server_with_an_env_token_is_not_bridged_and_says_why(data_dir: Path, capsys) -> None:
    held = bridge.claim_host("http://127.0.0.1:9/mcp", None, "1")
    try:
        assert bridge.auto_target({}) is None
        assert "token from the environment" in capsys.readouterr().err
    finally:
        held.close()


def _tool(name: str, read_only: bool):
    return SimpleNamespace(name=name, annotations=SimpleNamespace(read_only_hint=read_only))


def test_client_filter_applies_this_clients_read_only_and_toolsets() -> None:
    tools = [_tool("inspect", True), _tool("admin", False), _tool("build", False)]
    read_only = bridge.ClientFilter({"MESHTASTIC_MCP_READ_ONLY": "1"})
    assert [t.name for t in asyncio.run(read_only.list_tools(tools))] == ["inspect"]
    core = bridge.ClientFilter({"MESHTASTIC_MCP_TOOLSETS": "core"})
    assert [t.name for t in asyncio.run(core.list_tools(tools))] == ["inspect", "admin"]
    assert len(asyncio.run(bridge.ClientFilter({}).list_tools(tools))) == 2
    assert (
        len(asyncio.run(bridge.ClientFilter({"MESHTASTIC_MCP_TOOLSETS": "all"}).list_tools(tools)))
        == 3
    )


# -- end to end: a real `serve` on loopback, reached through the default stdio entry ---------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    port = _free_port()
    env = {
        **os.environ,
        "MESHTASTIC_MCP_DATA_DIR": str(data),
        "MESHTASTIC_MCP_TOKEN_FILE": str(token_file),
    }
    env.pop("MESHTASTIC_MCP_TOKEN", None)
    env.pop("MESHTASTIC_FIRMWARE_ROOT", None)
    # The server shows only `core`; a client that lists anything else did not bridge.
    server_env = {**env, "MESHTASTIC_MCP_TOOLSETS": "core"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meshtastic_mcp",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"serve exited: {proc.stderr.read() if proc.stderr else ''}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("serve did not start listening")
    yield SimpleNamespace(env=env, port=port, data=data)
    proc.terminate()
    proc.wait(timeout=20)


def _stdio(env: dict, extra: dict | None = None):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    return Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "meshtastic_mcp"],
            env={**env, **(extra or {})},
        ),
        timeout=60,
    )


def test_the_default_stdio_entry_bridges_to_the_hosts_server(served) -> None:
    info = json.loads((served.data / "serve.json").read_text())
    assert info["url"] == f"http://127.0.0.1:{served.port}/mcp"

    async def run() -> None:
        async with _stdio(served.env) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert "replay_start" not in tools  # the server's `core`, so bridged
            assert "lease" in tools["inspect"].input_schema["properties"]
            held = await client.call_tool(
                "claim", {"port": "tcp://bench-fake:4403", "holder": "bridge test"}
            )
            lease = held.structured_content["lease"]
            refused = await client.call_tool(
                "inspect", {"node": "tcp://bench-fake:4403"}, raise_on_error=False
            )
            assert refused.is_error and "bridge test" in refused.content[0].text
            await client.call_tool("release", {"lease": lease})
            listed = await client.call_tool("job", {})
            assert listed.structured_content == {"jobs": []}

        async with _stdio(served.env, {bridge.STANDALONE_ENV: "1"}) as client:
            assert "replay_start" in {t.name for t in await client.list_tools()}

        async with _stdio(served.env, {"MESHTASTIC_MCP_READ_ONLY": "1"}) as client:
            names = {t.name for t in await client.list_tools()}
            assert "inspect" in names and "claim" not in names and "admin" not in names

    asyncio.run(run())


def test_a_second_serve_on_the_host_refuses_to_start(served) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "meshtastic_mcp", "serve", "--port", str(_free_port())],
        env=served.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode != 0
    assert "already runs on this host" in out.stderr
