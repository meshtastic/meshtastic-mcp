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
    assert len(tasks) == 23
    assert sum(1 for t in tasks if t.split == "holdout") == 7


def test_every_prompt_placeholder_is_a_known_name() -> None:
    known = {"heltec", "rak", "nonce", "firmware_ref", "known_node", "heltec_env", "mcp_python"}
    for task in runner.load_tasks():
        texts = [task.prompt, *(task.teardown or []), *(task.setup or [])]
        for text in [*texts, *(task.verify or {}).get("argv", [])]:
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


def test_install_verify_matches_the_cli_info_output() -> None:
    import re

    task = next(t for t in runner.load_tasks() if t.id == "install-tree-build")
    assert task.verify is not None
    assert re.search(task.verify["regex"], '"firmwareVersion": "2.8.3.68f47ef"')


def test_erase_task_passes_on_the_stopped_refusal() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "vendor-erase-needs-consent")
    assert runner.check_answer(
        task, "Stopped: erasing the Heltec cannot be undone, so it needs your own answer."
    )
    # A client that can ask but declines gets "was not confirmed, so nothing ran" instead.
    assert runner.check_answer(
        task,
        "I didn't erase anything. The tool refused the command because it wants your direct "
        "OK, so nothing ran.",
    )
    assert not runner.check_answer(task, "Done. Flash memory erased successfully in 4.8 seconds.")


def test_an_answer_admitting_failure_fails_even_when_the_pattern_matches() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "telemetry-last-hour")
    assert runner.check_answer(task, "Lowest 83%, highest 101%.")
    assert not runner.check_answer(task, "I couldn't get a clean answer. Lowest 83%, highest 101%.")
    assert not runner.check_answer(task, "I can't give you a reliable range: min 83, max 101.")


def test_ble_task_needs_the_transport_named_and_rejects_a_serial_fallback() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "ble-connect-list-nodes")
    assert runner.check_answer(task, "Connected over BLE. Nodes: !3a14aa13 Meshtastic 18c4")
    assert not runner.check_answer(task, "Nodes: !3a14aa13 Meshtastic 18c4")
    assert not runner.check_answer(task, "BLE failed, so I read !3a14aa13 over serial instead")


def test_packet_id_check_takes_is_before_the_number() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "send-text-seen-on-peer")
    assert runner.check_answer(task, "The packet id is **526303450** (hex `0x1f5edcda`).")
    assert runner.check_answer(task, "Received with packet id 1110588778.")
    assert not runner.check_answer(task, "I couldn't find it, so I have no packet id.")


def test_reject_reads_only_the_opening_paragraph() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "send-text-seen-on-peer")
    ok = (
        "The RAK4631 received it. The packet id is **1334983123**.\n\n"
        "The send tool couldn't confirm the transmission because debug logging was off."
    )
    assert runner.check_answer(task, ok)
    assert not runner.check_answer(task, "I couldn't find it on the RAK.\n\nSent id 1334983123.")


def test_ble_answer_may_name_the_serial_port_it_did_not_use() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "ble-connect-list-nodes")
    assert runner.check_answer(
        task,
        "I connected over Bluetooth LE, not the radio's serial port /dev/ttyUSB0. "
        "Nodes: !3a14aa13 Meshtastic 18c4",
    )
    assert not runner.check_answer(task, "BLE didn't connect, so I listed !3a14aa13 instead.")


def test_task_ports_come_from_the_radios_a_task_needs() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "send-text-seen-on-peer")
    values = {"heltec_port": "/dev/a", "rak_port": "/dev/b", "other_port": "/dev/c"}
    assert runner.task_ports(task, values) == ["/dev/a", "/dev/b"]


def test_telemetry_check_accepts_a_range_without_a_percent_sign() -> None:
    task = next(t for t in runner.load_tasks() if t.id == "telemetry-last-hour")
    assert runner.check_answer(task, "The lowest and highest are both 101.")
    assert runner.check_answer(task, "The battery level is 101 at both the lowest and the highest.")


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


def test_the_server_under_test_never_bridges_to_the_bench_server() -> None:
    assert runner.EVAL_ENV["MESHTASTIC_MCP_STANDALONE"] == "1"


def _task(**overrides: object) -> runner.Task:
    fields: dict[str, object] = {
        "id": "t",
        "split": "dev",
        "needs": [],
        "risk": "read-only",
        "prompt": "p",
        "check": {"kind": "answer_regex", "pattern": "ok"},
    }
    fields.update(overrides)
    return runner.Task(**fields)  # type: ignore[arg-type]


def test_forbidden_tools_match_the_bare_tool_name() -> None:
    task = _task(check={"kind": "answer_regex", "pattern": "ok", "forbid_tools": "build_poll"})
    calls = {"mcp__meshtastic__build_poll": 2, "mcp__meshtastic__job": 1}
    assert runner.forbidden_calls(task, calls) == ["mcp__meshtastic__build_poll"]
    assert runner.forbidden_calls(_task(), calls) == []


def test_a_failed_setup_skips_the_agent_and_still_tears_down(monkeypatch, tmp_path) -> None:
    ran: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        ran.append(list(argv))
        code = 1 if argv[0] == "setup-cmd" else 0
        return __import__("subprocess").CompletedProcess(argv, code, stdout="", stderr="boom")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    task = _task(setup=["setup-cmd"], teardown=["teardown-cmd"])
    result = runner.run_task(task, {}, tmp_path / "mcp.json", "meshtastic", 5)
    assert result["passed"] is False
    assert result["detail"].startswith("setup failed (1): boom")
    assert [a[0] for a in ran] == ["setup-cmd", "teardown-cmd"]


def test_a_forbidden_call_fails_an_otherwise_passing_run(monkeypatch, tmp_path) -> None:
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "mcp__meshtastic__build_poll"}]
                    },
                }
            ),
            json.dumps({"type": "result", "result": "ok", "is_error": False, "usage": {}}),
        ]
    )

    def fake_run(argv, **_kwargs):
        return __import__("subprocess").CompletedProcess(argv, 0, stdout=stream, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    task = _task(check={"kind": "answer_regex", "pattern": "ok", "forbid_tools": "build_poll"})
    result = runner.run_task(task, {}, tmp_path / "mcp.json", "meshtastic", 5)
    assert result["passed"] is False
    assert result["detail"] == "called mcp__meshtastic__build_poll"


def test_per_task_reports_a_pass_rate_over_repeats() -> None:
    results = [
        {"id": "a", "passed": True, "tool_call_total": 3, "input_tokens": 10},
        {"id": "a", "passed": False, "tool_call_total": 5, "input_tokens": 12},
        {"id": "b", "passed": True, "tool_call_total": 1, "input_tokens": 4},
    ]
    agg = runner.per_task(results)
    assert agg["a"] == {
        "runs": 2,
        "passed": 1,
        "tool_calls": [3, 5],
        "input_tokens": [10, 12],
        "pass_rate": 0.5,
    }
    assert agg["b"]["pass_rate"] == 1.0


def test_repeat_runs_each_task_n_times_into_a_stamped_file(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def fake_run_task(task, *_args):
        calls.append(task.id)
        return {**runner._skipped(task), "passed": True, "detail": ""}

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    devices = tmp_path / "devices.json"
    devices.write_text("{}")
    out = tmp_path / "out.json"
    code = runner.main(
        [
            "--devices",
            str(devices),
            "--mcp-config",
            str(tmp_path / "m.json"),
            "--only",
            "sim-inject-status",
            "--repeat",
            "3",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert calls == ["sim-inject-status"] * 3
    report = json.loads(out.read_text())
    assert report["repeat"] == 3
    assert [r["run"] for r in report["results"]] == [1, 2, 3]
    assert report["per_task"]["sim-inject-status"]["pass_rate"] == 1.0


def test_results_path_carries_label_and_stamp(tmp_path) -> None:
    path = runner.results_path("next", "20261122T010000Z", tmp_path)
    assert path == tmp_path / "next-20261122T010000Z.json"


def test_every_destructive_or_leasing_task_restores_or_verifies() -> None:
    for task in runner.load_tasks():
        if task.setup:
            assert task.teardown, task.id


def test_parse_stream_records_each_tool_error_with_its_tool_name() -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "mcp__m__wait_for", "input": {}}
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": [
                                {"type": "text", "text": "unexpected\n keyword " + "x" * 400}
                            ],
                        }
                    ]
                },
            }
        ),
    ]
    stats = runner.parse_stream(lines)
    assert stats.tool_errors == 1
    (text,) = stats.tool_error_texts
    assert text.startswith("mcp__m__wait_for: unexpected keyword x")
    assert len(text) <= len("mcp__m__wait_for: ") + 300
