# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""GitHub issue delivery: every row of the failure-classification table, the
body-file round-trip, auto-create, and the single network retry — all against
a scripted fake ``gh`` dropped on PATH."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.db.database import Database
from meshtastic_mcp.web.services import github_issues as gi

FAKE_GH = r"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

spec = json.loads(Path(__file__).with_name("gh-spec.json").read_text())
log = Path(__file__).with_name("gh-calls.jsonl")
args = sys.argv[1:]
with log.open("a") as fh:
    fh.write(json.dumps(args) + "\n")

for entry in spec:
    prefix = entry["prefix"]
    if args[: len(prefix)] == prefix:
        n = entry.get("times")
        if n is not None:
            entry["times"] = n - 1
            if n <= 0:
            # exhausted — fall through to the next matching entry
                continue
        Path(__file__).with_name("gh-spec.json").write_text(json.dumps(spec))
        sys.stdout.write(entry.get("out", ""))
        sys.stderr.write(entry.get("err", ""))
        sys.exit(entry.get("rc", 0))
sys.exit(0)
"""

OK_PROBE = [
    {"prefix": ["--version"], "out": "gh version 2.40.0\n"},
    {"prefix": ["auth", "status"], "out": "Logged in\n"},
    {"prefix": ["api"], "out": "me/fleet-nightly\n"},
    {"prefix": ["label", "create"]},
]


@pytest.fixture()
def fake_gh(tmp_path: Path, monkeypatch):
    """Install a scripted gh on PATH; returns a handle to script + call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Handle:
        def script(self, entries: list[dict]) -> None:
            (bin_dir / "gh-spec.json").write_text(json.dumps(entries))

        def calls(self) -> list[list[str]]:
            log = bin_dir / "gh-calls.jsonl"
            if not log.exists():
                return []
            return [json.loads(ln) for ln in log.read_text().splitlines()]

    handle = Handle()
    handle.script([])
    return handle


def _cfg(**kw) -> gi.NightlyReportConfig:
    # Posting defaults OFF now; the delivery tests opt in explicitly.
    return gi.NightlyReportConfig(**{"repo": "me/fleet-nightly", "enabled": True, **kw})


def test_config_round_trip(tmp_path: Path):
    async def go():
        db = await Database(tmp_path / "registry.db").connect()
        cfg = _cfg(enabled=False, auto_create_repo=True)
        await gi.save_config(db, cfg)
        assert await gi.load_config(db) == cfg
        await db.close()

    asyncio.run(go())


def test_disabled_short_circuits(fake_gh):
    res = gi.post_issue(_cfg(enabled=False), title="t", body_md="b", labels=[])
    assert res.status == "disabled"
    assert fake_gh.calls() == []


def test_gh_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=[])
    assert res.status == "gh_missing"


def test_gh_unauthenticated(fake_gh):
    fake_gh.script(
        [
            {"prefix": ["--version"], "out": "gh version 2.40.0\n"},
            {"prefix": ["auth", "status"], "rc": 1, "err": "You are not logged in\n"},
        ]
    )
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=[])
    assert res.status == "gh_unauthenticated"


def test_repo_missing_without_autocreate(fake_gh):
    fake_gh.script(
        [
            {"prefix": ["--version"], "out": "gh version 2.40.0\n"},
            {"prefix": ["auth", "status"], "out": "ok\n"},
            {"prefix": ["api"], "rc": 1, "err": "HTTP 404: Not Found\n"},
        ]
    )
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=[])
    assert res.status == "repo_missing"
    assert not any(c[:2] == ["repo", "create"] for c in fake_gh.calls())


def test_repo_autocreate_then_post(fake_gh):
    fake_gh.script(
        [
            {"prefix": ["--version"], "out": "gh version 2.40.0\n"},
            {"prefix": ["auth", "status"], "out": "ok\n"},
            {"prefix": ["api"], "rc": 1, "err": "HTTP 404: Not Found\n"},
            {"prefix": ["repo", "create"], "out": "https://github.com/me/fleet-nightly\n"},
            {"prefix": ["label", "create"]},
            {
                "prefix": ["issue", "create"],
                "out": "https://github.com/me/fleet-nightly/issues/7\n",
            },
        ]
    )
    res = gi.post_issue(_cfg(auto_create_repo=True), title="t", body_md="b", labels=["nightly"])
    assert res.status == "posted"
    assert res.issue_url == "https://github.com/me/fleet-nightly/issues/7"
    assert any(c[:2] == ["repo", "create"] and "--private" in c for c in fake_gh.calls())


def test_forbidden(fake_gh):
    fake_gh.script(
        [
            {"prefix": ["--version"], "out": "gh version 2.40.0\n"},
            {"prefix": ["auth", "status"], "out": "ok\n"},
            {"prefix": ["api"], "rc": 1, "err": "HTTP 403 Forbidden\n"},
        ]
    )
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=[])
    assert res.status == "forbidden"


def test_post_body_file_and_labels(fake_gh):
    fake_gh.script(
        [
            *OK_PROBE,
            {
                "prefix": ["issue", "create"],
                "out": "https://github.com/me/fleet-nightly/issues/1\n",
            },
        ]
    )
    body = "# Nightly\n\nwith `code` and | pipes\n"
    res = gi.post_issue(_cfg(), title="Nightly", body_md=body, labels=["nightly", "failed"])
    assert res.status == "posted"
    create = next(c for c in fake_gh.calls() if c[:2] == ["issue", "create"])
    # Body went via --body-file (and was cleaned up after the call).
    body_arg = create[create.index("--body-file") + 1]
    assert not Path(body_arg).exists()
    assert create.count("--label") == 2
    labels = [c for c in fake_gh.calls() if c[:2] == ["label", "create"]]
    assert {c[2] for c in labels} == {"nightly", "failed"}


def test_network_error_single_retry(fake_gh):
    fake_gh.script(
        [
            *OK_PROBE,
            {
                "prefix": ["issue", "create"],
                "rc": 1,
                "err": "dial tcp: connect: network is unreachable\n",
                "times": 1,
            },
            {
                "prefix": ["issue", "create"],
                "out": "https://github.com/me/fleet-nightly/issues/2\n",
            },
        ]
    )
    slept = []
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=[], sleep=lambda s: slept.append(s))
    assert res.status == "posted" and slept == [10.0]
    creates = [c for c in fake_gh.calls() if c[:2] == ["issue", "create"]]
    assert len(creates) == 2


def test_label_rejection_retries_bare(fake_gh):
    fake_gh.script(
        [
            *OK_PROBE,
            {
                "prefix": ["issue", "create"],
                "rc": 1,
                "err": "could not add label: 'nightly' unexpected\n",
                "times": 1,
            },
            {
                "prefix": ["issue", "create"],
                "out": "https://github.com/me/fleet-nightly/issues/3\n",
            },
        ]
    )
    res = gi.post_issue(_cfg(), title="t", body_md="b", labels=["nightly"])
    assert res.status == "posted"
    creates = [c for c in fake_gh.calls() if c[:2] == ["issue", "create"]]
    assert "--label" in creates[0] and "--label" not in creates[1]


def test_check_gh_ok(fake_gh):
    fake_gh.script(OK_PROBE)
    probe = gi.check_gh("me/fleet-nightly")
    assert probe["ok"] and probe["repo_exists"] and probe["gh_version"].startswith("gh version")
