# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""`send_text(wait_for_tx=True)` must confirm against evidence that can exist.

The original implementation polled the recorder's *packet* stream for the
packet it had just sent. That stream is fed by the `meshtastic.receive` pubsub
topic (see `recorder/recorder.py`), which a self-originated packet never
reaches: the firmware echoes the packet back but omits the now-redundant `from`
field, and `MeshInterface._handlePacketFromRadio` treats a missing `from` as
"Device returned a packet we sent, ignoring" and returns before publishing.

So the check could never succeed for locally-originated traffic and reported
`tx_confirmed: false` even when the message was verifiably delivered (reproduced
on a T-Beam S3: the peer decoded the broadcast while the sender reported
failure; separately an RTL-SDR observed the 0.32 s burst on air while the
firmware-side check still said false). `rf_oracle.confirm_tx` documents the same
constraint for its `firmware_self_reported_tx` field.

The firmware does log the transmission, with the packet id in lowercase hex:

    enqueue for send (id=0x8738a6d9 ...)     <- queued, may still never key up
    Started Tx (id=0x8738a6d9 ...)           <- the radio actually transmitted

`Started Tx` is the signal that matters: the linger bug (PR #18) was precisely
a packet that got *enqueued* and then killed before airtime, so confirming on
the enqueue line would reintroduce a false positive.
"""

from __future__ import annotations

from typing import Any

import pytest

PID = 2268636889  # 0x8738a6d9 — a real id observed on hardware


def _log_line(text: str) -> dict[str, Any]:
    return {"ts": 1784837044.0, "port": "/dev/ttyACM1", "line": text}


@pytest.fixture
def stub_query(monkeypatch):
    """Stub the recorder queries; tests set `state` to shape what's visible.

    `server` is imported lazily (inside the fixture, not at module scope) —
    importing it at import time wires the recorder's pubsub subscriptions
    before conftest's session fixture registers its own, which makes pubsub
    reject the latter with a ListenerMismatchError. Same pattern as
    `test_mcp_surface.py`.
    """
    from meshtastic_mcp import server

    state: dict[str, Any] = {"log_lines": [], "packets": [], "server": server}

    def fake_logs_window(*_a, **kwargs):
        """Mirrors log_query.logs_window: grep is applied BEFORE the max_lines
        cap, and the cap keeps the most recent N. Emulating the cap is what
        makes `test_tx_line_survives_a_chatty_log` meaningful — without it the
        test would pass no matter how the lookup was implemented."""
        grep = kwargs.get("grep")
        max_lines = kwargs.get("max_lines", 200)
        lines = state["log_lines"]
        if grep is not None:
            import re

            rx = re.compile(grep)
            lines = [line for line in lines if rx.search(line["line"])]
        matched = len(lines)
        capped = lines[-max_lines:] if max_lines else lines
        return {
            "lines": capped,
            "total_matched": matched,
            "dropped": max(0, matched - max_lines),
        }

    def fake_packets_window(*_a, **_kw):
        return {
            "packets": state["packets"],
            "total_matched": len(state["packets"]),
            "dropped": state.get("packets_dropped", 0),
        }

    monkeypatch.setattr(server.log_query, "logs_window", fake_logs_window)
    monkeypatch.setattr(server.log_query, "packets_window", fake_packets_window)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    return state


def test_confirms_on_started_tx_log(stub_query):
    """The firmware's `Started Tx (id=0x...)` line confirms transmission."""
    stub_query["log_lines"] = [
        _log_line("enqueue for send (id=0x8738a6d9 fr=0xf8f277b5 to=0xffffffff)"),
        _log_line("Started Tx (id=0x8738a6d9 fr=0xf8f277b5 to=0xffffffff len=102)"),
    ]
    confirmed, _latency, reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 5.0)
    assert confirmed is True, f"expected confirmation from Started Tx, got {reason}"


def test_enqueue_alone_is_not_confirmation(stub_query):
    """`enqueue for send` without `Started Tx` must NOT confirm — that is exactly
    the PR #18 failure mode (queued, then killed before airtime)."""
    stub_query["log_lines"] = [
        _log_line("enqueue for send (id=0x8738a6d9 fr=0xf8f277b5 to=0xffffffff)"),
    ]
    confirmed, _latency, _reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 0.5)
    assert confirmed is False, "enqueue alone must not count as transmitted"


def test_other_packet_id_does_not_confirm(stub_query):
    """A `Started Tx` for a different packet must not confirm ours."""
    stub_query["log_lines"] = [_log_line("Started Tx (id=0xdeadbeef fr=0x1 to=0xffffffff)")]
    confirmed, _latency, _reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 0.5)
    assert confirmed is False


def test_rebroadcast_in_packet_stream_confirms(stub_query):
    """A neighbour's rebroadcast carries our packet id back to us — also proof
    it reached the air, even with no log capture."""
    stub_query["packets"] = [{"id": PID, "portnum": "TEXT_MESSAGE_APP"}]
    confirmed, _latency, _reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 5.0)
    assert confirmed is True


def test_no_log_capture_reports_unknown_not_failure(stub_query):
    """With no log lines captured at all there is no evidence channel, so the
    result must be `None` (unknown) — NOT `False`, which reads as 'it failed'.
    This is the regression that made a working mesh look broken."""
    stub_query["log_lines"] = []
    stub_query["packets"] = []
    confirmed, _latency, reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 0.5)
    assert confirmed is None, "no observability must be reported as unknown, not failure"
    assert reason and "debug_log_api" in reason, f"reason should point at the fix: {reason}"


def test_evidence_channel_present_but_no_tx_is_false(stub_query):
    """When logs ARE flowing and no TX line appears, `False` is a real signal."""
    stub_query["log_lines"] = [_log_line("Node status update: 0 online, 20 total")]
    confirmed, _latency, _reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 0.5)
    assert confirmed is False


def test_tx_line_survives_a_chatty_log(stub_query):
    """The TX line must be found even when swamped by unrelated firmware chatter.

    `logs_window` applies `grep` *before* its max_lines cap, so pushing the
    packet-id pattern down as the filter keeps the one line we need from being
    truncated away. An unfiltered 2 min window on real hardware measured
    total_matched=455 / dropped=395, so this is a live failure mode.
    """
    stub_query["log_lines"] = [_log_line(f"Node status update: {i} online") for i in range(500)]
    stub_query["log_lines"].append(_log_line("Started Tx (id=0x8738a6d9 fr=0x1 to=0xffffffff)"))
    stub_query["log_lines"] += [_log_line(f"Router: sniffing {i}") for i in range(500)]
    confirmed, _latency, reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 5.0)
    assert confirmed is True, f"grep-filtered lookup should survive log volume: {reason}"


def test_truncated_packet_window_is_flagged_when_unobservable(stub_query):
    """No log channel + a truncated packet window => we cannot rule out that a
    rebroadcast was dropped, so say so rather than implying clean observation."""
    stub_query["log_lines"] = []
    stub_query["packets"] = []
    stub_query["packets_dropped"] = 4200
    confirmed, _latency, reason = stub_query["server"]._confirm_tx(PID, "/dev/ttyACM1", 0.5)
    assert confirmed is None
    assert "truncated" in reason, f"truncation should be surfaced: {reason}"


def test_missing_packet_id_reports_unknown(stub_query):
    """Without a packet id there is nothing to match on — don't guess by
    matching 'any recent TEXT packet', which the old code did."""
    stub_query["log_lines"] = [_log_line("Started Tx (id=0x8738a6d9 fr=0x1 to=0xffffffff)")]
    confirmed, _latency, reason = stub_query["server"]._confirm_tx(None, "/dev/ttyACM1", 0.5)
    assert confirmed is None
    assert reason and "packet id" in reason
