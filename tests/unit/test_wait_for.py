# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""`wait_for`: hold a radio's connection and return when the recorder sees a match."""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import threading
import time
from pathlib import Path

import pytest

from meshtastic_mcp import recorder_query
from meshtastic_mcp.recorder_query import QueryError

RAK = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_7885014CB2C5B186-if00"


def _append(path: Path, row: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _text(ts: float, port: str = RAK, payload: str = "6576616c") -> dict:
    return {
        "ts": ts,
        "port": port,
        "from_node": "!3a14aa13",
        "portnum": "TEXT_MESSAGE_APP",
        "payload_hex_prefix": payload,
    }


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "packets.jsonl").write_text("")
    return tmp_path


def test_returns_as_soon_as_a_matching_row_arrives(base: Path) -> None:
    start = time.time()

    def arrive() -> None:
        time.sleep(0.6)
        _append(base / "packets.jsonl", _text(time.time()))

    threading.Thread(target=arrive).start()
    out = recorder_query.wait_for(
        "packets",
        "portnum = 'TEXT_MESSAGE_APP'",
        base=base,
        since=start,
        port=RAK,
        timeout_s=10,
        poll_s=0.1,
    )
    assert out["matched"] is True
    assert 0.5 <= out["waited_s"] < 5
    assert out["rows"][0][out["columns"].index("from_node")] == "!3a14aa13"


def test_times_out_without_a_match(base: Path) -> None:
    out = recorder_query.wait_for(
        "packets", "true", base=base, since=time.time(), timeout_s=1, poll_s=0.2
    )
    assert out["matched"] is False and out["rows"] == []
    assert out["waited_s"] >= 1


def test_rows_before_since_or_from_another_radio_do_not_match(base: Path) -> None:
    now = time.time()
    _append(base / "packets.jsonl", _text(now - 60))
    _append(base / "packets.jsonl", _text(now + 1, port="/dev/ttyUSB0"))
    out = recorder_query.wait_for(
        "packets", "true", base=base, since=now, port=RAK, timeout_s=1, poll_s=0.2
    )
    assert out["matched"] is False


def test_a_past_since_finds_an_event_that_already_arrived(base: Path) -> None:
    now = time.time()
    _append(base / "packets.jsonl", _text(now - 30))
    out = recorder_query.wait_for(
        "packets", "true", base=base, since=now - 120, port=RAK, timeout_s=1
    )
    assert out["matched"] is True and out["waited_s"] < 1


def test_archives_written_before_since_are_not_read(base: Path) -> None:
    old = base / "packets.20260101-000000-000000-00001.jsonl.gz"
    with gzip.open(old, "wt") as fh:
        fh.write(json.dumps(_text(time.time())) + "\n")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    files = recorder_query._stream_files(base, "packets", since=time.time() - 60)
    assert old not in files


@pytest.mark.parametrize(
    ("stream", "timeout", "match"),
    [("nodes", 5, "stream"), ("packets", 0, "timeout_s"), ("packets", 56, "timeout_s")],
)
def test_bad_arguments_are_rejected(base: Path, stream: str, timeout: float, match: str) -> None:
    with pytest.raises(QueryError, match=match):
        recorder_query.wait_for(stream, "true", base=base, since=0, timeout_s=timeout)


def test_tool_holds_the_connection_for_the_whole_wait(base: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from meshtastic_mcp.tools import core

    events: list[str] = []

    @contextlib.contextmanager
    def fake_connect(port=None, **_):
        events.append(f"open {port}")
        try:
            yield object()
        finally:
            events.append("close")

    def fake_wait(*args, **kwargs):
        events.append("wait")
        return {"matched": True, "rows": [[1]], "columns": ["ts"], "waited_s": 0.1}

    monkeypatch.setattr(core.connection, "resolve_port", lambda p: p or RAK)
    monkeypatch.setattr(core.connection, "connect", fake_connect)
    monkeypatch.setattr(core.recorder_query, "wait_for", fake_wait)
    monkeypatch.setattr(core, "get_recorder", lambda: SimpleNamespace(base_dir=base))
    out = core.wait_for("packets")
    assert events == [f"open {RAK}", "wait", "close"]
    assert out["port"] == RAK and out["matched"] is True
