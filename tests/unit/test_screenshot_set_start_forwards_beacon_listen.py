# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`screenshot_set_start` hands `beacon_listen` to the plan its job runs."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="module")
def server():
    # Deferred: importing `server` starts the recorder (see test_tool_annotations).
    from meshtastic_mcp import server as server_mod

    return server_mod


@pytest.mark.parametrize("beacon_listen", [True, False])
def test_screenshot_set_start_forwards_beacon_listen(
    server, monkeypatch, tmp_path, beacon_listen: bool
) -> None:
    from meshtastic_mcp import jobs
    from meshtastic_mcp import screenshot_set as ss

    plans: list[Any] = []
    make_plan = ss.make_plan

    def spy(*args: Any, **kwargs: Any) -> Any:
        plans.append(make_plan(*args, **kwargs))
        return plans[-1]

    monkeypatch.setattr(ss, "make_plan", spy)
    monkeypatch.setattr(jobs, "start", lambda kind, label, body: {"job_id": "j1"})
    monkeypatch.setenv("MESHTASTIC_MCP_CAPTURE_DIR", str(tmp_path))

    server.screenshot_set_start(["nodes"], out_dir="shots", beacon_listen=beacon_listen)

    assert [p.beacon_listen for p in plans] == [beacon_listen]
