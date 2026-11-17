# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A poll that reports a finished job always carries that job's whole log."""

from __future__ import annotations

import pathlib
import time

from meshtastic_mcp import jobs


def test_a_job_finishing_mid_poll_never_shows_done_with_a_stale_log(tmp_path, monkeypatch):
    log = tmp_path / "job.log"
    log.write_text("compiling\n", encoding="utf-8")
    state = {
        "job_id": "racejob",
        "kind": "builds",
        "label": "race",
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "artifacts": [],
        "log_path": str(log),
        "cancellable": False,
        "progress": jobs.Progress(),
    }
    monkeypatch.setitem(jobs._active, "racejob", state)

    real_read_text = pathlib.Path.read_text
    finished = []

    def read_then_finish(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if not finished:
            # The worker finishes right after this read: final log line first, then status.
            finished.append(True)
            log.write_text("compiling\nlinked firmware.bin\n", encoding="utf-8")
            with jobs.LOCK:
                state["exit_code"] = 0
                state["finished_at"] = time.time()
                state["status"] = "done"
        return text

    monkeypatch.setattr(pathlib.Path, "read_text", read_then_finish)

    for _ in range(2):
        polled = jobs.poll("racejob")
        if polled["status"] != "running":
            assert polled["log_tail"][-1] == "linked firmware.bin"
    assert polled["status"] == "done"
