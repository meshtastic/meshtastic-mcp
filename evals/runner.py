# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Score an MCP server against a fixed task set with ``claude -p``.

Each task in ``tasks.json`` is a prompt with a mechanical check. The runner starts one
``claude -p --mcp-config`` per task, reads the stream-json events, and records pass or fail,
tool calls by name, tool errors, tokens, cost, turns and wall time. A task that mutates a radio
or flashes it is skipped unless the matching ``--allow-*`` flag is given, and a mutating task's
teardown runs even when the agent fails.

    python -m evals.runner --list
    python -m evals.runner --devices devices.json --mcp-config bench.mcp.json --label master

``devices.json`` maps names to ports and values the prompts use, for example
``{"heltec": "Heltec V3", "heltec_port": "/dev/serial/by-id/...", "rak": "RAK4631", ...}``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASKS_PATH = Path(__file__).with_name("tasks.json")
RISKS = ("read-only", "mutating", "destructive")
ANSWER_CHARS = 2000  # enough of the final answer to judge a failed check by hand


@dataclass
class Task:
    id: str
    split: str
    needs: list[str]
    risk: str
    prompt: str
    check: dict[str, Any]
    verify: dict[str, Any] | None = None
    teardown: list[str] | None = None
    notes: str = ""


@dataclass
class RunStats:
    result_text: str = ""
    is_error: bool = False
    turns: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_errors: int = 0


def load_tasks(path: Path = TASKS_PATH) -> list[Task]:
    """Read and validate ``tasks.json``."""
    tasks = [Task(**raw) for raw in json.loads(path.read_text())]
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task ids")
    for t in tasks:
        if t.split not in ("dev", "holdout"):
            raise ValueError(f"{t.id}: split must be dev or holdout")
        if t.risk not in RISKS:
            raise ValueError(f"{t.id}: risk must be one of {RISKS}")
        if t.check.get("kind") != "answer_regex":
            raise ValueError(f"{t.id}: unknown check kind {t.check.get('kind')!r}")
    return tasks


def render(text: str, values: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders; an unknown name is an error, never left in the prompt."""

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(key)
        return values[key]

    return re.sub(r"\{(\w+)\}", sub, text)


def build_command(
    prompt: str, mcp_config: Path, server: str, max_turns: int, claude: str = "claude"
) -> list[str]:
    """The ``claude -p`` invocation for one task; only this server's tools are allowed."""
    return [
        claude,
        "-p",
        prompt,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        # Score the server, not the shell: no Bash, no user or plugin settings, no skills.
        # ToolSearch stays so deferred MCP tools can load, as in a normal session.
        "--restricted",
        "--disable-slash-commands",
        "--tools",
        "ToolSearch",
        "--allowedTools",
        f"mcp__{server}__*",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def parse_stream(lines: list[str]) -> RunStats:
    """Fold stream-json events into a :class:`RunStats`."""
    stats = RunStats()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    stats.tool_calls[block.get("name", "?")] += 1
        elif kind == "user":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                stats.tool_errors += sum(
                    1 for b in content if b.get("type") == "tool_result" and b.get("is_error")
                )
        elif kind == "result":
            usage = event.get("usage", {})
            stats.result_text = event.get("result", "") or ""
            stats.is_error = bool(event.get("is_error"))
            stats.turns = int(event.get("num_turns", 0))
            stats.duration_ms = int(event.get("duration_ms", 0))
            stats.cost_usd = float(event.get("total_cost_usd", 0.0))
            stats.input_tokens = sum(
                int(usage.get(key, 0) or 0)
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            stats.output_tokens = int(usage.get("output_tokens", 0))
    return stats


def check_answer(task: Task, result_text: str) -> bool:
    return re.search(task.check["pattern"], result_text) is not None


def verify_outcome(task: Task, values: dict[str, str]) -> tuple[bool, str]:
    """Run the out-of-band post-condition, when the task has one."""
    if not task.verify:
        return True, ""
    argv = [render(a, values) for a in task.verify["argv"]]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    ok = re.search(task.verify["regex"], proc.stdout + proc.stderr) is not None
    return ok, "" if ok else f"verify did not match {task.verify['regex']!r}"


def run_task(
    task: Task, values: dict[str, str], mcp_config: Path, server: str, max_turns: int
) -> dict[str, Any]:
    run_values = {**values, "nonce": uuid.uuid4().hex[:8]}
    prompt = render(task.prompt, run_values)
    started = time.monotonic()
    stats = RunStats()
    detail = ""
    passed = False
    try:
        proc = subprocess.run(
            build_command(prompt, mcp_config, server, max_turns),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        stats = parse_stream(proc.stdout.splitlines())
        answered = not stats.is_error and check_answer(task, stats.result_text)
        verified, detail = verify_outcome(task, run_values)
        passed = answered and verified
        if not answered and not detail:
            detail = "answer did not match" if not stats.is_error else "agent run errored"
    except subprocess.TimeoutExpired:
        detail = "timed out"
    finally:
        if task.teardown:
            subprocess.run(
                [render(a, run_values) for a in task.teardown],
                capture_output=True,
                timeout=60,
                check=False,
            )
    return {
        "id": task.id,
        "split": task.split,
        "passed": passed,
        "detail": detail,
        "wall_s": round(time.monotonic() - started, 1),
        "turns": stats.turns,
        "tool_calls": dict(stats.tool_calls),
        "tool_call_total": sum(stats.tool_calls.values()),
        "tool_errors": stats.tool_errors,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cost_usd": stats.cost_usd,
        "answer": stats.result_text[:ANSWER_CHARS],
    }


def select(
    tasks: list[Task], split: str, only: list[str], allow_mutating: bool, allow_destructive: bool
) -> list[Task]:
    """Tasks to run: the chosen split, minus risky ones the caller has not allowed."""
    chosen = [t for t in tasks if split == "all" or t.split == split]
    if only:
        chosen = [t for t in chosen if t.id in only]
    allowed = {"read-only"}
    if allow_mutating:
        allowed.add("mutating")
    if allow_destructive:
        allowed.add("destructive")
    return [t for t in chosen if t.risk in allowed]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    return {
        "tasks": n,
        "passed": sum(1 for r in results if r["passed"]),
        "tool_calls": sum(r["tool_call_total"] for r in results),
        "tool_errors": sum(r["tool_errors"] for r in results),
        "input_tokens": sum(r["input_tokens"] for r in results),
        "output_tokens": sum(r["output_tokens"] for r in results),
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "wall_s": round(sum(r["wall_s"] for r in results), 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print the tasks and exit")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument(
        "--only", action="append", default=[], help="run just this task id (repeatable)"
    )
    ap.add_argument("--allow-mutating", action="store_true")
    ap.add_argument("--allow-destructive", action="store_true")
    ap.add_argument("--devices", type=Path, help="JSON map of prompt values and ports")
    ap.add_argument("--mcp-config", type=Path, help="MCP config for the server under test")
    ap.add_argument("--server", default="meshtastic", help="server name in the MCP config")
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", type=Path, help="results file (default evals/results/<label>.json)")
    args = ap.parse_args(argv)

    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t.split:8} {t.risk:12} {t.id}")
        return 0
    if not args.devices or not args.mcp_config:
        ap.error("--devices and --mcp-config are required to run")

    values = json.loads(args.devices.read_text())
    chosen = select(tasks, args.split, args.only, args.allow_mutating, args.allow_destructive)
    results = []
    for task in chosen:
        try:
            results.append(run_task(task, values, args.mcp_config, args.server, args.max_turns))
        except KeyError as missing:
            results.append({**_skipped(task), "detail": f"devices.json has no {missing}"})
    report = {
        "label": args.label,
        "split": args.split,
        "summary": summarize(results),
        "results": results,
    }
    out = args.out or Path(__file__).with_name("results") / f"{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"]))
    return 0


def _skipped(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "split": task.split,
        "passed": False,
        "detail": "skipped",
        "wall_s": 0.0,
        "turns": 0,
        "tool_calls": {},
        "tool_call_total": 0,
        "tool_errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "answer": "",
    }


if __name__ == "__main__":
    sys.exit(main())
