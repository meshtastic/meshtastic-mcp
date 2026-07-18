# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly report delivery: post a markdown report as a GitHub issue via the
``gh`` CLI (which owns the token — no secrets pass through this process).

Every failure mode maps to a persisted status string the UI can badge and
hint on; nothing here ever raises into the nightly pipeline. All functions are
blocking — callers dispatch via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..db import repo_settings as rs
from ..db.database import Database

log = logging.getLogger("meshtastic_mcp.web.github_issues")

SETTINGS_KEY = "nightly_report"

GH_TIMEOUT = 30.0
_NETWORK_RETRY_DELAY_S = 10.0


@dataclass
class NightlyReportConfig:
    # Posting to GitHub is OFF by default — an external publish must be an
    # explicit operator opt-in (the report is always rendered + stored locally
    # regardless, and viewable in the UI). Turn it on in the Nightly tab.
    enabled: bool = False
    repo: str = "thebentern/fleet-nightly"
    auto_create_repo: bool = False
    max_body_kb: int = 60

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> NightlyReportConfig:
        d = d or {}
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


async def load_config(db: Database) -> NightlyReportConfig:
    return NightlyReportConfig.from_dict(await rs.get_json(db, SETTINGS_KEY))


async def save_config(db: Database, cfg: NightlyReportConfig) -> None:
    await rs.set_json(db, SETTINGS_KEY, asdict(cfg))


@dataclass
class DeliveryResult:
    status: str  # posted | disabled | gh_missing | gh_unauthenticated |
    #              repo_missing | forbidden | network_error | post_failed
    issue_url: str | None = None
    error: str | None = None


# Human-readable hints per status, surfaced by the UI next to the badge.
STATUS_HINTS = {
    "posted": "Report posted.",
    "disabled": "Posting is disabled — the report is stored locally only.",
    "gh_missing": "The gh CLI is not installed — run `meshtastic-mcp doctor` for "
    "the platform-specific install command.",
    "gh_unauthenticated": "gh is installed but not logged in. Run: gh auth login",
    "repo_missing": "The report repo does not exist. Create it with: "
    "gh repo create <repo> --private (or enable auto_create_repo).",
    "forbidden": "The gh token lacks access to the report repo.",
    "network_error": "Could not reach GitHub — will need a repost.",
    "post_failed": "gh issue create failed — see error.",
}


def _gh(*args: str, timeout: float = GH_TIMEOUT) -> tuple[int, str, str]:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout, out.stderr
    except FileNotFoundError:
        return 127, "", "gh: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout:.0f}s"


def _tail(text: str, limit: int = 500) -> str:
    return (text or "").strip()[-limit:]


def check_gh(repo: str) -> dict:
    """Probe gh presence → auth → repo visibility. The Datadog ``test_key``
    analog behind ``POST /api/nightly/test``."""
    rc, out, err = _gh("--version")
    if rc == 127:
        return {"ok": False, "status": "gh_missing", "error": _tail(err)}
    version = out.splitlines()[0].strip() if out else None

    rc, out, err = _gh("auth", "status")
    if rc != 0:
        return {
            "ok": False,
            "status": "gh_unauthenticated",
            "gh_version": version,
            "error": _tail(err or out),
        }

    rc, out, err = _gh("api", f"repos/{repo}", "--jq", ".full_name")
    if rc != 0:
        blob = f"{out}\n{err}".lower()
        status = "forbidden" if "403" in blob or "forbidden" in blob else "repo_missing"
        return {
            "ok": False,
            "status": status,
            "gh_version": version,
            "repo_exists": False,
            "error": _tail(err or out),
        }
    return {"ok": True, "status": "ok", "gh_version": version, "repo_exists": True}


def _classify_failure(rc: int, out: str, err: str) -> str:
    blob = f"{out}\n{err}".lower()
    if rc == 127:
        return "gh_missing"
    if "not logged in" in blob or "auth login" in blob:
        return "gh_unauthenticated"
    # Only strong repo signals — a bare "not found" (e.g. a rejected label)
    # must NOT masquerade as repo_missing, which would suppress the label-free
    # retry and mislead the operator.
    if "could not resolve to a repository" in blob or "http 404" in blob:
        return "repo_missing"
    if "403" in blob or "forbidden" in blob:
        return "forbidden"
    network = (
        rc == 124
        or "timed out" in blob
        or ("could not resolve" in blob and "host" in blob)
        or "connect" in blob
        or "network" in blob
        or "dial tcp" in blob
    )
    return "network_error" if network else "post_failed"


def _retry_safe(rc: int, out: str, err: str) -> bool:
    """True only for failures that prove the request NEVER reached GitHub, so a
    retry cannot create a duplicate issue. A timeout is ambiguous — the issue
    may already have been created — so it is NOT retry-safe."""
    blob = f"{out}\n{err}".lower()
    if rc == 124 or "timed out" in blob or "timeout" in blob:
        return False
    return (
        ("could not resolve" in blob and "host" in blob)
        or "connection refused" in blob
        or "network is unreachable" in blob
        or "no route to host" in blob
        or "dial tcp" in blob
    )


def _create_repo(repo: str) -> tuple[bool, str]:
    rc, out, err = _gh(
        "repo",
        "create",
        repo,
        "--private",
        "--description",
        "FleetSuite nightly bake reports",
        timeout=60.0,
    )
    return rc == 0, _tail(err or out)


def _ensure_labels(repo: str, labels: list[str]) -> bool:
    ok = True
    colors = {
        "nightly": "1d76db",
        "green": "0e8a16",
        "failed": "d93f0b",
        "pipeline-broken": "b60205",
    }
    for name in labels:
        rc, _out, _err = _gh(
            "label", "create", name, "-R", repo, "--force", "--color", colors.get(name, "ededed")
        )
        ok = ok and rc == 0
    return ok


def post_issue(
    cfg: NightlyReportConfig,
    *,
    title: str,
    body_md: str,
    labels: list[str],
    sleep=time.sleep,
) -> DeliveryResult:
    """Create the issue. Body goes via a temp file (argv limits); labels are
    best-effort — if labeling fails the post is retried without them. A failure
    is retried once ONLY when it proves the request never reached GitHub (so a
    retry can't create a duplicate issue); a timeout is left as network_error
    for the operator to repost."""
    if not cfg.enabled:
        return DeliveryResult(status="disabled")

    probe = check_gh(cfg.repo)
    if not probe["ok"]:
        if probe["status"] == "repo_missing" and cfg.auto_create_repo:
            created, err = _create_repo(cfg.repo)
            if not created:
                return DeliveryResult(status="repo_missing", error=err)
        else:
            return DeliveryResult(status=probe["status"], error=probe.get("error"))

    labels_ok = _ensure_labels(cfg.repo, labels)

    # Securely created, uniquely named temp file — never a predictable path in
    # a shared /tmp (symlink/race hardening).
    fd, tmp_name = tempfile.mkstemp(prefix="nightly-report-", suffix=".md")
    body_file = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body_md)
        base = [
            "issue",
            "create",
            "--repo",
            cfg.repo,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        with_labels = base.copy()
        for name in labels:
            with_labels += ["--label", name]

        def attempt(args: list[str]) -> DeliveryResult:
            result = DeliveryResult(status="post_failed", error="no attempt ran")
            for try_no in (1, 2):
                rc, out, err = _gh(*args, timeout=60.0)
                if rc == 0:
                    url = out.strip().splitlines()[-1] if out.strip() else None
                    return DeliveryResult(status="posted", issue_url=url)
                result = DeliveryResult(
                    status=_classify_failure(rc, out, err), error=_tail(err or out)
                )
                if try_no == 1 and _retry_safe(rc, out, err):
                    sleep(_NETWORK_RETRY_DELAY_S)
                    continue
                break
            return result

        res = attempt(with_labels if labels_ok else base)
        if res.status == "post_failed" and labels_ok and labels:
            # A label rejection shouldn't lose the report — retry bare.
            res = attempt(base)
        return res
    finally:
        try:
            body_file.unlink(missing_ok=True)
        except OSError:
            log.debug("could not remove %s", body_file)
