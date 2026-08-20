# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CotRelay: capture to disk + manifest, and N-way relay between clients."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from meshtastic_mcp.replay.cot_relay import CotRelay

# A bare <event> (the canonical stream form) ...
PLI_EVENT = (
    b'<event version="2.0" uid="ANDROID-1" type="a-f-G-U-C" time="2026-08-20T14:00:00Z" '
    b'start="2026-08-20T14:00:00Z" stale="2026-08-20T14:06:00Z" how="h-g-i-g-o">'
    b'<point lat="41.6" lon="-93.7" hae="250.0" ce="5.0" le="9999999.0"/>'
    b'<detail><contact callsign="EARP"/></detail></event>'
)
# ... and the same event with an <?xml?> declaration prefix (ATAK-CIV sends this
# over a plain TCP stream). The relay must capture the bare element from either.
PLI = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + PLI_EVENT
PING_EVENT = (
    b'<event version="2.0" uid="ANDROID-1-ping" type="t-x-c-t" time="2026-08-20T14:00:01Z" '
    b'start="2026-08-20T14:00:01Z" stale="2026-08-20T14:00:11Z" how="m-g">'
    b'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/><detail/></event>'
)
PING = b'<?xml version="1.0"?>' + PING_EVENT


def _connect(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(5)
    return s


def _recv_until(sock: socket.socket, token: bytes, timeout: float = 5.0) -> bytes:
    buf = b""
    deadline = time.time() + timeout
    while token not in buf and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)
    assert predicate()


def test_capture_writes_files_and_manifest(tmp_path: Path) -> None:
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        a.sendall(PLI + PING)  # back-to-back, no separator
        _wait(lambda: relay.seq >= 2)
        a.close()

    files = sorted(p.name for p in tmp_path.glob("*.xml"))
    assert files == ["0001_a-f-G-U-C.xml", "0002_t-x-c-t.xml"]
    # Declaration stripped: the bare <event> element is captured.
    assert (tmp_path / "0001_a-f-G-U-C.xml").read_bytes() == PLI_EVENT

    records = [json.loads(ln) for ln in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert [r["type"] for r in records] == ["a-f-G-U-C", "t-x-c-t"]
    assert records[0]["callsign"] == "EARP"
    assert records[0]["bytes"] == len(PLI_EVENT)


def test_capture_bare_event_without_declaration(tmp_path: Path) -> None:
    # The canonical stream form is a bare <event> (repo's tak_server.py sends
    # this); the relay must capture it, not only the <?xml?>-prefixed form.
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        a.sendall(PLI_EVENT + PING_EVENT)
        _wait(lambda: relay.seq >= 2)
        a.close()
    assert (tmp_path / "0001_a-f-G-U-C.xml").read_bytes() == PLI_EVENT
    assert (tmp_path / "0002_t-x-c-t.xml").read_bytes() == PING_EVENT


def test_relay_broadcasts_to_other_peers_only(tmp_path: Path) -> None:
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        b = _connect(relay.port)
        _wait(lambda: relay.status()["peer_count"] == 2)

        a.sendall(PLI)
        got_b = _recv_until(b, b"</event>")
        assert got_b == PLI_EVENT  # relayed as the bare element to the other peer

        # The sender must NOT get its own event echoed back.
        a.settimeout(0.5)
        try:
            echoed = a.recv(4096)
        except TimeoutError:
            echoed = b""
        assert echoed == b""
        a.close()
        b.close()


def test_status_counts_and_partial_frames(tmp_path: Path) -> None:
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        # Send one event split across two writes — must still frame correctly.
        a.sendall(PLI[:50])
        time.sleep(0.2)
        a.sendall(PLI[50:])
        _wait(lambda: relay.seq == 1)

        st = relay.status()
        assert st["running"] is True
        assert st["events_captured"] == 1
        assert st["type_counts"] == {"a-f-G-U-C": 1}
        assert st["peer_count"] == 1
        # status surfaces the peer's callsign (not just its IP) for a legible view.
        assert st["peers"][0]["callsign"] == "EARP"
        a.close()


def test_capture_dir_numbering_resumes(tmp_path: Path) -> None:
    (tmp_path / "0001_a-f-G-U-C.xml").write_bytes(PLI)
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        a.sendall(PING)
        _wait(lambda: relay.seq >= 2)
        a.close()
    assert (tmp_path / "0002_t-x-c-t.xml").exists()


def test_numbering_resumes_from_max_not_count(tmp_path: Path) -> None:
    # A gap (deleted middle file) must not let the next event reuse a live
    # number: with only 0003 present, count()==1 would reuse seq 2, but the
    # highest-prefix logic continues at 4.
    (tmp_path / "0003_a-f-G-U-C.xml").write_bytes(PLI_EVENT)
    with CotRelay(outdir=tmp_path, port=0) as relay:
        a = _connect(relay.port)
        a.sendall(PING)
        _wait(lambda: relay.seq >= 4)
        a.close()
    assert (tmp_path / "0004_t-x-c-t.xml").exists()
    assert (tmp_path / "0003_a-f-G-U-C.xml").read_bytes() == PLI_EVENT  # not overwritten
