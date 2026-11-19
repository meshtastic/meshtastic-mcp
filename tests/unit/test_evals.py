# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""The eval harness: task file shape, prompt rendering, stream parsing and task selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals import runner

STREAM = [
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__meshtastic__device_info", "input": {}},
                    {"type": "text", "text": "checking"},
                ]
            },
        }
    ),
    json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "is_error": True, "content": "port busy"}]
            },
        }
    ),
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__meshtastic__device_info", "input": {}}
                ]
            },
        }
    ),
    "not json",
    json.dumps(
        {
            "type": "result",
            "result": "Firmware 2.8.3 on HELTEC_V3",
            "is_error": False,
            "num_turns": 3,
            "duration_ms": 4200,
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 100,
                "output_tokens": 120,
            },
        }
    ),
]


def test_task_file_is_valid_and_holds_five_back() -> None:
    tasks = runner.load_tasks()
    assert len(tasks) == 15
    assert sum(1 for t in tasks if t.split == "holdout") == 5


def test_every_prompt_placeholder_is_a_known_name() -> None:
    known = {"heltec", "rak", "nonce", "firmware_ref", "known_node"}
    for task in runner.load_tasks():
        for text in [task.prompt, *(task.teardown or []), *(task.verify or {}).get("argv", [])]:
            for name in __import__("re").findall(r"\{(\w+)\}", text):
                assert name in known or name.endswith("_port") or name.endswith("_owner"), (
                    task.id,
                    name,
                )


def test_render_substitutes_and_rejects_unknown_names() -> None:
    assert runner.render("send {x} to {y}", {"x": "a", "y": "b"}) == "send a to b"
    with pytest.raises(KeyError):
        runner.render("send {missing}", {})


def test_parse_stream_counts_tools_errors_and_usage() -> None:
    stats = runner.parse_stream(STREAM)
    assert stats.tool_calls["mcp__meshtastic__device_info"] == 2
    assert stats.tool_errors == 1
    assert (stats.turns, stats.duration_ms) == (3, 4200)
    assert (stats.input_tokens, stats.output_tokens) == (900, 120)
    assert stats.result_text.startswith("Firmware 2.8.3")
    assert not stats.is_error


def test_check_answer_matches_the_task_pattern() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "device-summary")
    assert runner.check_answer(task, "Heltec: HELTEC_V3, US, 2.8.3")
    assert not runner.check_answer(task, "I could not reach the radio")


def test_an_answer_admitting_failure_fails_even_when_the_pattern_matches() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "telemetry-last-hour")
    assert runner.check_answer(task, "Lowest 83%, highest 101%.")
    assert not runner.check_answer(task, "I couldn't get a clean answer. Lowest 83%, highest 101%.")


def test_ble_task_needs_the_transport_named_and_rejects_a_serial_fallback() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "ble-connect-list-nodes")
    assert runner.check_answer(task, "Connected over BLE. Nodes: !3a14aa13 Meshtastic 18c4")
    assert not runner.check_answer(task, "Nodes: !3a14aa13 Meshtastic 18c4")
    assert not runner.check_answer(task, "BLE failed, so I read !3a14aa13 over serial instead")


def test_task_ports_come_from_the_radios_a_task_needs() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "send-text-seen-on-peer")
    values = {"heltec_port": "/dev/a", "rak_port": "/dev/b", "other_port": "/dev/c"}
    assert runner.task_ports(task, values) == ["/dev/a", "/dev/b"]


def test_telemetry_check_accepts_a_range_without_a_percent_sign() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "telemetry-last-hour")
    assert runner.check_answer(task, "The lowest and highest are both 101.")


def test_unscored_tasks_stay_out_of_the_pass_count() -> None:
    results = [
        {
            "scored": True,
            "passed": True,
            "tool_call_total": 1,
            "tool_errors": 0,
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": 0.0,
            "wall_s": 1.0,
        },
        {
            "scored": False,
            "passed": True,
            "tool_call_total": 1,
            "tool_errors": 0,
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": 0.0,
            "wall_s": 1.0,
        },
    ]
    summary = runner.summarize(results)
    assert (summary["tasks"], summary["scored"], summary["passed"]) == (2, 1, 1)


def test_select_filters_by_split_and_risk() -> None:
    tasks = runner.load_tasks()
    safe = runner.select(tasks, "dev", [], allow_mutating=False, allow_destructive=False)
    assert safe and all(t.risk == "read-only" and t.split == "dev" for t in safe)
    mutating = runner.select(tasks, "all", [], allow_mutating=True, allow_destructive=False)
    assert any(t.risk == "mutating" for t in mutating)
    assert all(t.risk != "destructive" for t in mutating)
    only = runner.select(
        tasks, "all", ["device-summary"], allow_mutating=False, allow_destructive=False
    )
    assert [t.id for t in only] == ["device-summary"]


def test_build_command_limits_tools_to_the_server_under_test() -> None:
    cmd = runner.build_command("hi", Path("bench.mcp.json"), "meshtastic", 10)
    assert cmd[:3] == ["claude", "-p", "hi"]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__meshtastic__*"
    assert cmd[cmd.index("--tools") + 1] == "ToolSearch"
    assert "--restricted" in cmd
    assert "--strict-mcp-config" in cmd


def test_summarize_totals_results() -> None:
    results = [
        {
            "passed": True,
            "tool_call_total": 2,
            "tool_errors": 0,
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": 0.1,
            "wall_s": 3.0,
        },
        {
            "passed": False,
            "tool_call_total": 1,
            "tool_errors": 1,
            "input_tokens": 20,
            "output_tokens": 6,
            "cost_usd": 0.2,
            "wall_s": 4.0,
        },
    ]
    summary = runner.summarize(results)
    assert (summary["tasks"], summary["passed"], summary["tool_errors"]) == (2, 1, 1)
    assert summary["input_tokens"] == 30
