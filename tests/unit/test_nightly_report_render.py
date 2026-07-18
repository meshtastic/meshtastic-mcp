# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Report rendering: title variants, section structure, folding/escaping, the
size-budget ladder, and the reporter's persist-always contract."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db import repo_nightly as rn
from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import nightly_report as nrep
from meshtastic_mcp.web.services.nightly_analysis import AnalysisResult, Failure, Observation

NIGHTLY = {
    "id": 5,
    "run_id": 12,
    "started_at": 1_784_500_000.0,
    "finished_at": 1_784_512_000.0,
    "status": "failed",
    "step": "done",
    "fw_sha_before": "a" * 40,
    "fw_sha_after": "b" * 40,
}


def _analysis(**kw) -> AnalysisResult:
    res = AnalysisResult()
    res.counts = {"passed": 42, "failed": 0, "skipped": 1, "exit_code": 0}
    for k, v in kw.items():
        setattr(res, k, v)
    return res


def test_title_variants():
    green = _analysis()
    assert "green — 42 passed" in nrep.render_title(dict(NIGHTLY, status="passed"), green)
    assert "(develop @ bbbbbbb)" in nrep.render_title(dict(NIGHTLY, status="passed"), green)

    red = _analysis(
        counts={"passed": 40, "failed": 3, "skipped": 0},
        observations=[
            Observation("warn", "panic", "x"),
            Observation("info", "version_change", "y"),
        ],
    )
    title = nrep.render_title(NIGHTLY, red)
    assert "3 failed, 1 observations" in title

    broken = nrep.render_title(dict(NIGHTLY, status="error", step="firmware_update"), _analysis())
    assert "PIPELINE FAILED at firmware_update" in broken


def test_failed_night_with_zero_test_failures_is_not_green():
    # status=failed but 0 recorded test failures (collection error / infra exit)
    # must NOT render green.
    analysis = _analysis(counts={"passed": 0, "failed": 0, "skipped": 0, "exit_code": 2})
    title = nrep.render_title(dict(NIGHTLY, status="failed"), analysis)
    assert "green" not in title and "suite failed" in title
    assert nrep.render_labels(dict(NIGHTLY, status="failed"), analysis) == ["nightly", "failed"]
    # canceled likewise never green.
    assert "canceled" in nrep.render_title(dict(NIGHTLY, status="canceled"), analysis)
    assert nrep.render_labels(dict(NIGHTLY, status="canceled"), analysis) == ["nightly", "failed"]


def test_pipeline_failed_title_names_the_real_step():
    # nightly.step has advanced to handoff by report time; the culprit comes
    # from the observation trail.
    obs = [
        {"step": "firmware_update", "kind": "step.failed", "severity": "error", "message": "boom"},
        {"step": "handoff", "kind": "report.failed", "severity": "error", "message": "x"},
    ]
    culprit = nrep.failed_step(obs)
    assert culprit == "firmware_update"
    title = nrep.render_title(dict(NIGHTLY, status="error", step="handoff"), _analysis(), culprit)
    assert "PIPELINE FAILED at firmware_update" in title


def test_labels():
    assert nrep.render_labels(dict(NIGHTLY, status="passed"), _analysis()) == ["nightly", "green"]
    red = _analysis(counts={"failed": 1, "passed": 0, "skipped": 0})
    assert nrep.render_labels(NIGHTLY, red) == ["nightly", "failed"]
    assert nrep.render_labels(dict(NIGHTLY, status="error"), _analysis()) == [
        "nightly",
        "pipeline-broken",
    ]


def _steps_obs() -> list[dict]:
    return [
        {
            "step": "firmware_update",
            "kind": "step.finished",
            "severity": "info",
            "message": "ok",
            "data": {"duration_s": 63.0},
        },
        {
            "step": "suite",
            "kind": "step.failed",
            "severity": "error",
            "message": "exit 1",
            "data": {"duration_s": 7200.0},
        },
        {
            "step": "soak",
            "kind": "soak.summary",
            "severity": "info",
            "message": "done",
            "data": {
                "duration_s": 7200.0,
                "lines": {"S1": 900},
                "sends_attempted": 12,
                "sends_failed": 1,
                "snapshots": 8,
                "preflight_failures": 0,
            },
        },
    ]


def test_body_sections_and_folding():
    longrepr = "\n".join(f"line {i}" for i in range(120)) + "\nE assert False"
    analysis = _analysis(
        counts={"passed": 10, "failed": 1, "skipped": 0},
        failures=[Failure("tests/mesh/test_a.py::test_x[rak]", "mesh", "S1", 3.0, longrepr)],
        observations=[
            Observation("warn", "panic", "S1: 2 panic markers", device="S1", evidence=["*** boom"]),
            Observation(
                "info",
                "version_change",
                "firmware develop moved aaaaaaa → bbbbbbb",
                evidence=["abc123 Fix"],
                data={"compare_url": "https://github.com/meshtastic/firmware/compare/a...b"},
            ),
        ],
        device_rows=[
            {
                "device": "board|one",  # pipe must be escaped in the table
                "env": "rak4631",
                "online": True,
                "bake": "passed",
                "soak_lines": 900,
                "panics": 1,
                "errors": 0,
            }
        ],
    )
    body = nrep.render_body(NIGHTLY, analysis, _steps_obs(), max_body_kb=60)

    for section in (
        "## Summary",
        "## Versions",
        "## Steps",
        "## Failures (1)",
        "## Soak",
        "## Observations",
        "## Fleet",
    ):
        assert section in body, section
    assert body.index("## Summary") < body.index("## Failures (1)") < body.index("## Fleet")
    # Longrepr folded head+tail inside a details fence.
    assert "<details><summary>traceback</summary>" in body
    assert "lines omitted" in body and "E assert False" in body
    # Steps table carries the failure note; soak stats render.
    assert "FAILED" in body and "exit 1" in body
    assert "12 sent, 1 send failures" in body
    # Escaping + compare link + untrusted footer.
    assert "board\\|one" in body
    assert "[compare](https://github.com/meshtastic/firmware/compare/a...b)" in body
    assert "untrusted content" in body


def test_behavior_section_separated():
    analysis = _analysis(
        observations=[
            Observation("info", "behavior", "summary", evidence=["- all quiet"]),
            Observation("warn", "traffic_loss", "1/2 lost"),
        ]
    )
    body = nrep.render_body(dict(NIGHTLY, status="passed"), analysis, [], max_body_kb=60)
    assert "## Behavioral analysis (local model)" in body
    assert "Draft from an offline model" in body
    # traffic_loss lands in Observations, not the behavior section.
    assert body.index("behavior") < body.index("traffic_loss")


def test_budget_ladder_compacts_then_truncates():
    big_longrepr = "x" * 2500
    failures = [
        Failure(f"tests/mesh/test_bulk.py::test_{i}", "mesh", None, 1.0, big_longrepr)
        for i in range(30)
    ]
    analysis = _analysis(counts={"passed": 0, "failed": 30, "skipped": 0}, failures=failures)

    full = nrep.render_body(NIGHTLY, analysis, [], max_body_kb=1024)
    assert full.count("<details><summary>traceback</summary>") == nrep.FULL_FAILURE_DETAILS

    fitted = nrep.render_body(NIGHTLY, analysis, [], max_body_kb=8)
    assert len(fitted) <= 8 * 1024
    if "…truncated" in fitted:
        assert fitted.rstrip().endswith("_")  # marker + footer still intact
    else:
        # Compact mode alone fit the budget — every failure is a one-liner.
        assert "<details><summary>traceback</summary>" not in fitted


def test_fence_widens_for_embedded_backticks():
    # The fence must be strictly longer than the longest backtick run inside.
    assert nrep._fence("evil\n```\ninjection").startswith("````")
    assert nrep._fence("has ```` four").startswith("`````")
    assert nrep._fence("plain").startswith("```\n")


def test_gps_scrubbed_from_report_evidence():
    # Device-authored log lines with GPS must be redacted in the rendered body
    # (Scrubber("redact") is applied by the analyzer; verify the report path
    # doesn't leak coordinates from an observation's evidence).
    from meshtastic_mcp.web.services.scrub import Scrubber

    raw = "INFO | GPS lat=37.7749123 lon=-122.4194155 fix"
    scrubbed = Scrubber("redact").scrub(raw)
    assert "37.7749123" not in scrubbed and "-122.4194155" not in scrubbed
    analysis = _analysis(
        observations=[Observation("warn", "panic", "gps leak", evidence=[scrubbed])]
    )
    body = nrep.render_body(dict(NIGHTLY, status="passed"), analysis, [], max_body_kb=60)
    assert "37.7749123" not in body and "-122.4194155" not in body


def test_reporter_persists_even_when_disabled(tmp_path, monkeypatch):
    """publish() with posting disabled still renders + stores the report."""

    class FakeHub:
        def __init__(self):
            self.frames = []

        def publish(self, topic, data):
            self.frames.append((topic, data))

    async def go():
        db = await Database(tmp_path / "db").connect()
        nid = await rn.create(db, scheduled_for=0.0)
        await rn.finish(db, nid, status="passed", summary=None)

        from meshtastic_mcp.web.services import github_issues as gi
        from meshtastic_mcp.web.services import nightly_analysis as na

        await gi.save_config(db, gi.NightlyReportConfig(enabled=False))
        monkeypatch.setattr(na, "has_local_model", lambda: False)
        monkeypatch.setattr(nrep, "nightly_data_dir", lambda _id: tmp_path / "no-such-night")

        hub = FakeHub()
        reporter = nrep.NightlyReporter(db, hub)
        report = await reporter.publish(nid)

        assert report is not None and report["status"] == "disabled"
        assert report["title"] and report["body_md"].startswith("## Summary")
        assert hub.frames and hub.frames[0][1]["status"] == "disabled"
        await db.close()

    asyncio.run(go())
