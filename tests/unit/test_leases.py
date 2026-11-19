# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Device leases: the shared store, the per-call check, jobs, and the tools."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meshtastic_mcp import jobs, leases
from meshtastic_mcp.leases import LeaseError

PORT = "tcp://bench-a:4403"


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_claim_check_release() -> None:
    held = leases.claim(PORT, ttl_s=60, holder="eval")
    leases.check(PORT, held["lease"])
    with pytest.raises(LeaseError, match="leased to eval"):
        leases.check(PORT, None)
    with pytest.raises(LeaseError, match="leased to eval"):
        leases.claim(PORT)
    assert leases.release(held["lease"])["holder"] == "eval"
    leases.check(PORT, None)


def test_an_unleased_device_works_without_a_handle_but_rejects_a_stray_one() -> None:
    leases.check(PORT, None)
    with pytest.raises(LeaseError, match="claim it first"):
        leases.check(PORT, "lease_bogus")


def test_a_lease_ends_at_its_ttl_and_cannot_be_renewed_after() -> None:
    held = leases.claim(PORT, ttl_s=1)
    time.sleep(1.1)
    leases.check(PORT, None)
    with pytest.raises(LeaseError, match="expired"):
        leases.claim(PORT, lease=held["lease"])
    assert leases.active() == []


def test_renewing_extends_the_same_handle() -> None:
    held = leases.claim(PORT, ttl_s=5)
    again = leases.claim(PORT, ttl_s=600, lease=held["lease"])
    assert again["lease"] == held["lease"]
    assert again["expires_at"] > held["expires_at"]


@pytest.mark.parametrize("ttl", [0, leases.MAX_TTL_S + 1])
def test_ttl_is_bounded(ttl: int) -> None:
    with pytest.raises(LeaseError, match="ttl_s"):
        leases.claim(PORT, ttl_s=ttl)


def test_a_symlink_and_its_target_are_one_device(tmp_path: Path) -> None:
    target = tmp_path / "ttyACM9"
    target.write_text("")
    link = tmp_path / "usb-RAKwireless_WisCore_RAK4631_Board-if00"
    link.symlink_to(target)
    held = leases.claim(str(link))
    with pytest.raises(LeaseError):
        leases.check(str(target), None)
    leases.check(str(target), held["lease"])


def test_break_lease_drops_another_holder() -> None:
    leases.claim(PORT, holder="stuck")
    assert leases.break_lease(PORT)["holder"] == "stuck"
    leases.check(PORT, None)


_CLAIM_IN_CHILD = """
import sys
from meshtastic_mcp import leases
try:
    print(leases.claim(sys.argv[1], ttl_s=60, holder=sys.argv[2])["lease"])
except leases.LeaseError:
    print("refused")
"""


def _claim_in_child(holder: str, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CLAIM_IN_CHILD, PORT, holder],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_a_lease_taken_in_another_process_is_seen_here(store: Path) -> None:
    import os

    env = {**os.environ, "MESHTASTIC_MCP_DATA_DIR": str(store)}
    out, _ = _claim_in_child("other-session", env).communicate(timeout=60)
    assert out.strip().startswith("lease_")
    with pytest.raises(LeaseError, match="other-session"):
        leases.check(PORT, None)


def test_concurrent_claims_from_several_processes_have_one_winner(store: Path) -> None:
    import os

    env = {**os.environ, "MESHTASTIC_MCP_DATA_DIR": str(store)}
    procs = [_claim_in_child(f"p{i}", env) for i in range(6)]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert sum(o.startswith("lease_") for o in outs) == 1
    assert outs.count("refused") == 5


def test_a_job_carries_its_lease_and_renews_it(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_LEASE_RENEW_S", 0.2)
    held = leases.claim(PORT, ttl_s=1)
    seen: dict = {}

    def body(state, log_path):
        seen["lease"] = leases.CURRENT.get()
        time.sleep(1.5)
        log_path.write_text("done")
        state["status"] = "done"

    token = leases.CURRENT.set(held["lease"])
    try:
        job = jobs.start("test", "lease job", body)
    finally:
        leases.CURRENT.reset(token)
    time.sleep(1.2)
    # Past the 1 s TTL, the job's renewals keep the lease live.
    with pytest.raises(LeaseError):
        leases.check(PORT, None)
    deadline = time.time() + 5
    while jobs.poll(job["job_id"])["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert seen["lease"] == held["lease"]


# -- the server: the `lease` argument, the per-call check, `claim` and `release` ------------


@pytest.fixture(scope="module")
def server():
    from meshtastic_mcp import server as server_mod

    return server_mod


def _call(server, name: str, args: dict):
    from fastmcp import Client

    async def run():
        async with Client(server.app) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def test_device_tools_take_a_lease_and_other_port_tools_do_not(server) -> None:
    tools = {t.name: t for t in asyncio.run(server.app.list_tools())}
    has_lease = {n for n, t in tools.items() if "lease" in t.parameters.get("properties", {})}
    for name in ("device_info", "set_config", "reboot", "send_text", "get_config"):
        assert name in has_lease, name
    for name in server._PORT_NOT_A_DEVICE - {"claim"}:
        if name in tools:
            assert name not in has_lease, name
    assert tools["device_info"].annotations.read_only_hint is True


def test_a_device_call_on_another_sessions_lease_is_refused(server, monkeypatch) -> None:
    monkeypatch.setattr(server.info, "device_info", lambda **_: {"port": PORT})
    held = leases.claim(PORT, holder="eval run")
    refused = _call(server, "device_info", {"port": PORT})
    assert refused.is_error and "leased to eval run" in refused.content[0].text
    ok = _call(server, "device_info", {"port": PORT, "lease": held["lease"]})
    assert ok.is_error is False, ok.content


def test_claim_and_release_tools(server, monkeypatch) -> None:
    out = _call(server, "claim", {"port": PORT, "holder": "me", "ttl_s": 30})
    assert out.is_error is False, out.content
    lease = out.structured_content["lease"]
    assert _call(server, "claim", {"port": PORT}).is_error
    released = _call(server, "release", {"lease": lease})
    assert released.is_error is False and released.structured_content["holder"] == "me"
    assert _call(server, "release", {"lease": lease}).is_error


def test_cli_lists_and_breaks_leases(store: Path, capsys) -> None:
    from meshtastic_mcp import __main__ as cli

    leases.claim(PORT, holder="stuck")
    with pytest.raises(SystemExit) as listed:
        cli.main(["leases"])
    assert listed.value.code == 0
    assert "stuck" in capsys.readouterr().out
    with pytest.raises(SystemExit) as broke:
        cli.main(["leases", "--break", PORT])
    assert broke.value.code == 0
    assert leases.active() == []
