# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`query`: read-only SQL over the recorder's JSONL streams."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from meshtastic_mcp import recorder_query
from meshtastic_mcp.recorder_query import QueryError


def _rows(path: Path, rows: list[dict], tail: str = "") -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    data = tmp_path / ".mtlog"
    data.mkdir()
    with gzip.open(data / "telemetry.20260101-000000-000000-00001.jsonl.gz", "wt") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": 1.0,
                    "port": "/dev/a",
                    "from_node": "!aaaa",
                    "variant": "device",
                    "fields": {"battery_level": 90},
                }
            )
            + "\n"
        )
    # An integer node number and a string id in one column, then a half-written last line.
    _rows(
        data / "telemetry.jsonl",
        [
            {
                "ts": 2.0,
                "port": "/dev/a",
                "from_node": 3735928559,
                "variant": "device",
                "fields": {"battery_level": 80},
            },
            {
                "ts": 3.0,
                "port": "/dev/a",
                "from_node": "!bbbb",
                "variant": "device",
                "fields": {"battery_level": 101},
            },
        ],
        tail='{"ts": 4.0, "port": "/dev/a", "from_n',
    )
    (data / "logs.jsonl").write_text("")
    return data


def test_reads_archives_and_live_file_with_fixed_types(base: Path) -> None:
    out = recorder_query.run(
        "SELECT from_node, CAST(fields->>'battery_level' AS INT) AS b FROM telemetry ORDER BY ts",
        base=base,
    )
    assert out["columns"] == ["from_node", "b"]
    assert out["rows"] == [["!aaaa", 90], ["3735928559", 80], ["!bbbb", 101]]
    assert out["truncated"] is False


def test_every_view_exists_when_its_files_are_missing_or_empty(base: Path) -> None:
    for view in ("logs", "packets", "events"):
        out = recorder_query.run(f"SELECT count(*) FROM {view}", base=base)
        assert out["rows"] == [[0]]


def test_time_column_is_derived_from_ts(base: Path) -> None:
    out = recorder_query.run("SELECT time FROM telemetry ORDER BY ts LIMIT 1", base=base)
    assert out["rows"][0][0] == "1970-01-01T00:00:01+00:00"


def test_row_cap_reports_truncation(base: Path) -> None:
    out = recorder_query.run("SELECT ts FROM telemetry", base=base, max_rows=2)
    assert out["row_count"] == 2
    assert out["truncated"] is True


@pytest.mark.parametrize("max_rows", [0, recorder_query.MAX_ROWS_CEILING + 1])
def test_row_cap_out_of_range_is_rejected(base: Path, max_rows: int) -> None:
    with pytest.raises(QueryError, match="max_rows"):
        recorder_query.run("SELECT 1", base=base, max_rows=max_rows)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "CREATE TABLE t AS SELECT 1",
        "COPY (SELECT 1) TO 'out.csv'",
        "ATTACH 'other.db'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "PRAGMA version",
    ],
)
def test_anything_but_one_select_is_rejected(base: Path, sql: str) -> None:
    with pytest.raises(QueryError):
        recorder_query.run(sql, base=base)


def test_files_outside_the_data_directory_are_unreadable(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(QueryError, match=r"(?i)permission"):
        recorder_query.run(f"SELECT * FROM read_text('{outside}')", base=base)


def test_globbing_outside_the_data_directory_is_refused(base: Path) -> None:
    with pytest.raises(QueryError, match=r"(?i)permission"):
        recorder_query.run("SELECT * FROM glob('/etc/*')", base=base)


def test_memory_and_threads_are_capped_and_locked(base: Path) -> None:
    out = recorder_query.run(
        "SELECT current_setting('memory_limit'), current_setting('threads')", base=base
    )
    limit, threads = out["rows"][0]
    assert limit.replace(" ", "").upper().startswith(("488.2MIB", "512.0MIB", "512MB"))
    assert threads == recorder_query.THREADS
    with pytest.raises(QueryError):
        recorder_query.run("SET memory_limit='8GB'", base=base)


def test_a_slow_query_is_interrupted(base: Path) -> None:
    with pytest.raises(QueryError, match="exceeded"):
        recorder_query.run(
            "SELECT count(*) FROM range(10000000000) a",
            base=base,
            timeout_s=0.2,
        )


def test_schema_lists_every_view_with_time() -> None:
    schema = recorder_query.schema()
    assert set(schema) == {"logs", "telemetry", "packets", "events"}
    assert all(cols["ts"] == "DOUBLE" and cols["time"] == "TIMESTAMPTZ" for cols in schema.values())


def test_tool_raises_tool_error_on_a_rejected_statement(base: Path, monkeypatch) -> None:
    from fastmcp.exceptions import ToolError

    from meshtastic_mcp.tools import core

    monkeypatch.setattr(core, "get_recorder", lambda: type("R", (), {"base_dir": base})())
    assert core.query("SELECT count(*) FROM telemetry")["rows"] == [[3]]
    with pytest.raises(ToolError, match="SELECT"):
        core.query("DELETE FROM telemetry")
