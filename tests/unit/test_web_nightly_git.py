# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly git step helpers.

``firmware_update`` runs against real scratch git repos (cheap, and the reset
semantics are the whole point). ``self_update`` uses an injected fake command
runner — exercising the pull/reinstall/rollback decision tree without touching
pip or npm."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web.services import nightly


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A scratch upstream with a ``develop`` branch and one tracked file."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "userPrefs.jsonc").write_text("{}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_firmware_update_clones_then_hard_resets(origin: Path, tmp_path: Path):
    clone = tmp_path / "nightly-firmware"

    res = nightly.firmware_update(clone, url=str(origin), branch="develop")
    assert res.ok and res.cloned and res.sha_after == _git(origin, "rev-parse", "HEAD")

    # Upstream moves; the local tracked file is left dirty (crashed bake).
    (origin / "userPrefs.jsonc").write_text('{"new": true}\n')
    _git(origin, "commit", "-am", "tip moves")
    tip = _git(origin, "rev-parse", "HEAD")
    (clone / "userPrefs.jsonc").write_text("garbage from a crashed bake")
    (clone / "stray.log").write_text("untracked leftover")

    res = nightly.firmware_update(clone, url=str(origin), branch="develop")
    assert res.ok and not res.cloned
    assert res.sha_before != tip and res.sha_after == tip
    # Hard reset healed the tracked file; untracked leftovers survive, reported.
    assert (clone / "userPrefs.jsonc").read_text() == '{"new": true}\n'
    assert (clone / "stray.log").exists() and "stray.log" in res.untracked


def test_firmware_update_fetch_failure_reports_old_sha(origin: Path, tmp_path: Path):
    clone = tmp_path / "nightly-firmware"
    assert nightly.firmware_update(clone, url=str(origin), branch="develop").ok
    sha = _git(clone, "rev-parse", "HEAD")

    def failing(cmd, cwd, timeout):
        if "fetch" in cmd:
            return 1, "", "network down"
        return nightly._run(cmd, cwd, timeout)

    res = nightly.firmware_update(clone, url=str(origin), branch="develop", runner=failing)
    assert not res.ok and res.sha_before == sha and res.error is not None
    assert "fetch" in res.error


def test_firmware_update_reclones_broken_checkout(origin: Path, tmp_path: Path):
    # Simulate an interrupted first clone: a `.git` that is not a usable repo.
    clone = tmp_path / "nightly-firmware"
    (clone / ".git").mkdir(parents=True)
    (clone / "leftover").write_text("half-clone junk")

    res = nightly.firmware_update(clone, url=str(origin), branch="develop")
    assert res.ok and res.cloned  # wiped and re-cloned from scratch
    assert res.sha_after == _git(origin, "rev-parse", "HEAD")
    assert (clone / "userPrefs.jsonc").exists()  # a real checkout now
    assert not (clone / "leftover").exists()


def test_commit_subjects_caps(origin: Path, tmp_path: Path):
    clone = tmp_path / "clone"
    nightly.firmware_update(clone, url=str(origin), branch="develop")
    old = _git(origin, "rev-parse", "HEAD")
    for i in range(4):
        (origin / "f.txt").write_text(str(i))
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", f"change {i}")
    nightly.firmware_update(clone, url=str(origin), branch="develop")
    new = _git(clone, "rev-parse", "HEAD")

    subjects = nightly.commit_subjects(clone, old, new, limit=3)
    assert len(subjects) == 4 and subjects[-1] == "… +1 more"
    assert subjects[0].endswith("change 3")


class FakeRunner:
    """Scripted command runner: first matching prefix wins; records calls."""

    def __init__(self, script: list[tuple[tuple[str, ...], tuple[int, str, str]]]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd, timeout) -> tuple[int, str, str]:
        self.calls.append(cmd)
        for prefix, result in self.script:
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return 0, "", ""

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


def _self_update_script(overrides: dict | None = None):
    """Happy-path script for self_update; override entries by key prefix."""
    base = {
        ("git", "rev-parse", "HEAD"): (0, "oldsha\n", ""),
        ("git", "fetch"): (0, "", ""),
        ("git", "rev-list"): (0, "2\n", ""),
        ("git", "diff"): (0, "src/meshtastic_mcp/web/app.py\n", ""),
        ("git", "pull"): (0, "", ""),
    }
    base.update(overrides or {})
    return list(base.items())


def test_self_update_no_op_when_up_to_date(tmp_path: Path):
    runner = FakeRunner(_self_update_script({("git", "rev-list"): (0, "0\n", "")}))
    res = nightly.self_update(tmp_path, runner=runner)
    assert res.ok and not res.updated and res.sha_after == "oldsha"
    assert not runner.ran("git", "pull")


def test_self_update_pull_and_reinstall(tmp_path: Path):
    runner = FakeRunner(_self_update_script())
    res = nightly.self_update(tmp_path, runner=runner, python="py")
    assert res.ok and res.updated and not res.spa_changed
    assert runner.ran("git", "pull", "--ff-only", "origin", "master")
    assert runner.ran("py", "-m", "pip", "install", "-e")


def test_self_update_spa_diff_classification(tmp_path: Path):
    diff = "web-ui/src/App.vue\nweb-ui/package-lock.json\npyproject.toml\n"
    runner = FakeRunner(_self_update_script({("git", "diff"): (0, diff, "")}))
    # Pretend the npm build succeeded and produced the fresh static dir.
    fresh = tmp_path / "src" / "meshtastic_mcp" / "web" / "static.new"

    def with_build(cmd, cwd, timeout):
        if cmd[:3] == ["npm", "run", "build"]:
            fresh.mkdir(parents=True, exist_ok=True)
            (fresh / "index.html").write_text("new")
        return runner(cmd, cwd, timeout)

    res = nightly.self_update(tmp_path, runner=with_build, python="py")
    assert res.ok and res.updated and res.spa_changed and res.deps_changed
    assert res.spa_built
    assert runner.ran("npm", "ci")  # lockfile changed
    static = tmp_path / "src" / "meshtastic_mcp" / "web" / "static"
    assert (static / "index.html").read_text() == "new"


def test_self_update_refuses_dirty_tree(tmp_path: Path):
    # A dirty checkout must be left completely untouched — no pull, no reset —
    # so the rollback path can never destroy uncommitted work.
    runner = FakeRunner(
        _self_update_script({("git", "status", "--porcelain"): (0, " M foo.py\n", "")})
    )
    res = nightly.self_update(tmp_path, runner=runner, python="py")
    assert not res.ok and res.dirty and res.sha_before == "oldsha"
    assert not runner.ran("git", "pull")
    assert not runner.ran("git", "reset")
    assert not runner.ran("py", "-m", "pip")


def test_self_update_pip_failure_rolls_back(tmp_path: Path):
    runner = FakeRunner(_self_update_script({("py", "-m", "pip"): (1, "", "resolver exploded")}))
    res = nightly.self_update(tmp_path, runner=runner, python="py")
    assert not res.ok and res.rolled_back and res.error is not None
    assert "pip install" in res.error
    assert runner.ran("git", "reset", "--hard", "oldsha")


def test_self_update_spa_build_failure_keeps_old_static(tmp_path: Path):
    static = tmp_path / "src" / "meshtastic_mcp" / "web" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("old")
    diff = "web-ui/src/App.vue\n"
    runner = FakeRunner(
        _self_update_script(
            {
                ("git", "diff"): (0, diff, ""),
                ("npm", "run", "build"): (1, "", "vite exploded"),
            }
        )
    )
    res = nightly.self_update(tmp_path, runner=runner, python="py")
    assert res.ok and res.updated and not res.spa_built
    assert res.spa_error is not None and "vite exploded" in res.spa_error
    assert (static / "index.html").read_text() == "old"
