# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only SQL over the recorder's JSONL streams.

Each call opens a fresh in-memory DuckDB connection, defines one view per stream over its
gzipped archives and live file, then locks the connection to the recorder directory before
running the caller's statement. Only a single SELECT runs: no file access outside the data
directory, no COPY, ATTACH, INSTALL or settings changes.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Column types are fixed rather than sniffed: `from_node` is a string id or an integer node
# number depending on the packet, and a sniffed type would reject one of them.
_COMMON = {"ts": "DOUBLE", "port": "VARCHAR", "role": "VARCHAR"}
STREAMS: dict[str, dict[str, str]] = {
    "logs": {
        **_COMMON,
        "level": "VARCHAR",
        "tag": "VARCHAR",
        "uptime_s": "BIGINT",
        "line": "VARCHAR",
        "heap_free": "BIGINT",
        "heap_total": "BIGINT",
        "heap_delta": "BIGINT",
        "heap_event": "JSON",
    },
    "telemetry": {
        **_COMMON,
        "from_node": "VARCHAR",
        "variant": "VARCHAR",
        "fields": "JSON",
        "device_time": "DOUBLE",
        "source": "VARCHAR",
    },
    "packets": {
        **_COMMON,
        "from_node": "VARCHAR",
        "to_node": "VARCHAR",
        "portnum": "VARCHAR",
        "hop_limit": "INTEGER",
        "want_ack": "BOOLEAN",
        "rx_rssi": "DOUBLE",
        "rx_snr": "DOUBLE",
        "channel": "INTEGER",
        "id": "BIGINT",
        "payload_size": "INTEGER",
        "payload_hex_prefix": "VARCHAR",
    },
    "events": {
        **_COMMON,
        "kind": "VARCHAR",
        "label": "VARCHAR",
        "note": "VARCHAR",
        "data": "JSON",
    },
}

DEFAULT_MAX_ROWS = 200
MAX_ROWS_CEILING = 5000
DEFAULT_TIMEOUT_S = 20.0
MEMORY_LIMIT = "512MB"
THREADS = 2


class QueryError(ValueError):
    """The statement was rejected or failed; the message says why."""


def _stream_files(base: Path, stream: str) -> list[Path]:
    # Archive names sort chronologically; the live file is newest. Empty files are skipped
    # because DuckDB cannot infer anything from them and the columns are fixed anyway.
    files = sorted(base.glob(f"{stream}.*.jsonl.gz"))
    live = base / f"{stream}.jsonl"
    if live.exists():
        files.append(live)
    return [f for f in files if f.stat().st_size > 0]


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _view_sql(base: Path, stream: str) -> str:
    columns = STREAMS[stream]
    files = _stream_files(base, stream)
    if not files:
        nulls = ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in columns.items())
        return f"SELECT {nulls}, CAST(NULL AS TIMESTAMPTZ) AS time WHERE false"
    file_list = "[" + ", ".join(_sql_str(str(f)) for f in files) + "]"
    struct = "{" + ", ".join(f"{_sql_str(c)}: {_sql_str(t)}" for c, t in columns.items()) + "}"
    # The writer appends while we read, so the live file can end in a partial line, which
    # `ignore_errors` turns into an all-NULL row; `ts IS NOT NULL` drops it.
    return (
        f"SELECT *, to_timestamp(ts) AS time FROM read_json({file_list}, "
        f"format='newline_delimited', columns={struct}, ignore_errors=true) "
        "WHERE ts IS NOT NULL"
    )


def schema() -> dict[str, dict[str, str]]:
    """Each view's columns and DuckDB types, including the derived `time` column."""
    return {name: {**cols, "time": "TIMESTAMPTZ"} for name, cols in STREAMS.items()}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def run(
    sql: str,
    *,
    base: Path,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one read-only SELECT over the recorder views at `base`.

    Returns `{columns, rows, row_count, truncated}`. `rows` holds at most `max_rows` rows;
    `truncated` is true when the statement produced more.
    """
    import duckdb

    if not 1 <= max_rows <= MAX_ROWS_CEILING:
        raise QueryError(f"max_rows must be between 1 and {MAX_ROWS_CEILING}")
    con = duckdb.connect(":memory:")
    try:
        try:
            statements = con.extract_statements(sql)
        except duckdb.Error as exc:
            raise QueryError(f"could not parse the query: {exc}") from exc
        if len(statements) != 1:
            raise QueryError("pass exactly one SQL statement")
        if statements[0].type != duckdb.StatementType.SELECT:
            raise QueryError("only SELECT statements are allowed")

        base = base.resolve()
        con.execute("SET TimeZone='UTC'")
        # The bench host is shared; DuckDB would otherwise take 80% of RAM and every core.
        con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
        con.execute(f"SET threads={THREADS}")
        for stream in STREAMS:
            con.execute(f"CREATE TEMP VIEW {stream} AS {_view_sql(base, stream)}")
        con.execute(f"SET allowed_directories=[{_sql_str(str(base))}]")
        con.execute("SET enable_external_access=false")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        con.execute("SET lock_configuration=true")

        timer = threading.Timer(timeout_s, con.interrupt)
        timer.start()
        try:
            cur = con.execute(sql)
            fetched = cur.fetchmany(max_rows + 1)
        except duckdb.InterruptException as exc:
            msg = f"query exceeded {timeout_s:g}s; narrow it with a WHERE on ts"
            raise QueryError(msg) from exc
        except duckdb.Error as exc:
            raise QueryError(str(exc)) from exc
        finally:
            timer.cancel()
        columns = [d[0] for d in cur.description or []]
    finally:
        con.close()

    truncated = len(fetched) > max_rows
    rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
