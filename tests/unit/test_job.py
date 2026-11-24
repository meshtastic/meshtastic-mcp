# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`job`: one tool over the job registry, returning each kind's own poll view."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp.exceptions import ToolError

from meshtastic_mcp import jobs


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_MCP_DATA_DIR", str(tmp_path))
    from meshtastic_mcp.tools import core as core_mod

    return core_mod


def _sleeper(seconds: float):
    def body(state, log_path):
        time.sleep(seconds)
        log_path.write_text("line 1\nline 2\n")
        with jobs.LOCK:
            state["status"] = "done"
            state["exit_code"] = 0
            state["finished_at"] = time.time()

    return body


def test_every_job_kind_has_its_poller_registered(core) -> None:
    from meshtastic_mcp import ble_sniffer, flash, vanity  # noqa: F401  (registers pollers)

    for kind in ("builds", "flashes", "grinds", "ble-captures", "display-drives"):
        assert kind in jobs._pollers, kind


def test_a_registered_kind_gets_its_own_view(core, monkeypatch) -> None:
    monkeypatch.setitem(
        jobs._pollers, "widgets", lambda job_id, tail: {**jobs.poll(job_id, tail), "extra": 1}
    )
    started = jobs.start("widgets", "w", _sleeper(0))
    out = asyncio.run(core.job(started["job_id"], wait_s=5))
    assert out["status"] == "done" and out["extra"] == 1


def test_an_unregistered_kind_falls_back_to_the_plain_poll(core) -> None:
    started = jobs.start("screenshot_set", "s", _sleeper(0))
    out = asyncio.run(core.job(started["job_id"], wait_s=5, tail_lines=1))
    assert out["status"] == "done" and out["log_tail"] == ["line 2"]


def test_wait_returns_when_the_job_ends_not_at_the_deadline(core) -> None:
    started = jobs.start("widgets-fast", "w", _sleeper(0.4))
    t = time.monotonic()
    out = asyncio.run(core.job(started["job_id"], wait_s=20))
    assert out["status"] == "done"
    assert time.monotonic() - t < 5


def test_wait_gives_up_at_the_deadline_with_the_job_still_running(core) -> None:
    started = jobs.start("widgets-slow", "w", _sleeper(3))
    t = time.monotonic()
    out = asyncio.run(core.job(started["job_id"], wait_s=0.5))
    assert out["status"] == "running"
    assert 0.4 < time.monotonic() - t < 2.5


def test_unknown_id_and_bad_wait_are_tool_errors(core) -> None:
    with pytest.raises(ToolError, match="Unknown job_id"):
        asyncio.run(core.job("nope"))
    started = jobs.start("widgets", "w", _sleeper(0))
    with pytest.raises(ToolError, match="wait_s"):
        asyncio.run(core.job(started["job_id"], wait_s=60))


def test_the_listing_never_hands_out_a_grind_id(core) -> None:
    from meshtastic_mcp import vanity  # noqa: F401  (registers grinds as private)

    grind = jobs.start("grinds", "pattern dc80", _sleeper(0))
    listed = {j["job_id"] for j in asyncio.run(core.job())["jobs"]}
    assert grind["job_id"] not in listed
    assert asyncio.run(core.job(grind["job_id"], wait_s=5))["job_id"] == grind["job_id"]


def test_no_id_lists_jobs_newest_first(core) -> None:
    first = jobs.start("widgets", "first", _sleeper(0))
    time.sleep(0.01)
    second = jobs.start("widgets", "second", _sleeper(0))
    ids = [j["job_id"] for j in asyncio.run(core.job())["jobs"]]
    assert ids.index(second["job_id"]) < ids.index(first["job_id"])
