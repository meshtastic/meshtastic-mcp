# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture + relay server for real ATAK/iTAK Cursor-on-Target (CoT) traffic.

A plain-TCP CoT endpoint that TAK clients connect to as an unauthenticated
"streaming" input (no SSL, no enrollment). It does two things at once:

* **Capture** — every discrete ``<event>...</event>`` a client streams is
  written to ``<outdir>/NNNN_<type>.xml`` and appended to ``manifest.jsonl``
  (one JSON line per event: seq, wall-clock time, peer, cot_type, callsign,
  bytes). This is the corpus of *real* CoT shapes — what ATAK actually emits,
  including detail children (``<takv>``, ``<status>``, ``<track>``,
  ``<precisionlocation>``, ``<_flow-tags_>``) that spec-derived fixtures omit.

* **Relay** — each event is rebroadcast to every *other* connected client, so
  two or more plain streaming clients see each other as contacts (self-PLI,
  markers, GeoChat) without a real TAK Server. There is no per-contact
  addressing or auth: everything is broadcast to everyone, the same semantics
  as the CoT UDP multicast group this replaces (``239.2.3.1:6969``).

Why this exists (learnings baked in):

* A single silent client is dropped by ATAK's own link health check about every
  2.5 min (it re-sends its self-PLI on reconnect, so nothing is lost). With two
  clients relaying each other's traffic the link isn't silent, so this is rarer.
* **GeoChat only transmits when the client believes it has a peer.** Broadcast
  GeoChat to "All Chat Rooms" on a lone client stays local and never hits the
  wire — you need at least two relayed clients to capture ``b-t-f`` chat.
* ``t-x-c-t`` (TAK client keepalive ping) is emitted every few seconds and is
  *not* in the TAKPacket-SDK fixture corpus — a real gap this captures.

Pure stdlib, so this is part of the always-on core (no capability gate).
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

# Capture filenames are ``NNNN_<type>.xml`` — parse the leading sequence number.
_SEQ_RE = re.compile(r"^(\d+)_")

# CoT stream framing. A client may send a bare `<event>...</event>` (the
# canonical stream form — the repo's own tak_server.py strips the `<?xml?>`
# declaration and documents "CoT stream events are bare elements") OR prefix an
# `<?xml?>` declaration (ATAK-CIV over a plain TCP stream does this). Events
# arrive back-to-back with no separator, so we frame by finding each `<event`
# start and its `</event>` end — matching either form — and capture the bare
# element (dropping any declaration, so the corpus is uniform).
_EVENT_START = b"<event"
_EVENT_END = b"</event>"
_TYPE_RE = re.compile(rb'\btype="([^"]+)"')
# Station identity, in order of trust: the <contact> element (PLI, markers),
# then GeoChat's senderCallsign. A bare callsign= would also match
# <marti><dest callsign> — the *recipient* — and mislabel chat events.
_CALLSIGN_RES = (
    re.compile(rb'<contact\b[^>]*\bcallsign="([^"]+)"'),
    re.compile(rb'\bsenderCallsign="([^"]+)"'),
)

# A single event above this size is almost certainly a framing error (unbounded
# buffer growth from a client that never closes an <event>); cap defensively.
_MAX_EVENT_BYTES = 1 << 20  # 1 MiB


@dataclass
class _Peer:
    ident: int
    addr: str
    sock: socket.socket
    events: int = 0
    callsign: str = ""  # last non-empty callsign seen from this peer (PLI/marker)


@dataclass
class CotRelay:
    """Threaded capture + N-way relay CoT server.

    Usage::

        relay = CotRelay(outdir="captures/session1", port=8087)
        relay.start()               # background thread; clients connect to host:port
        ...                         # drive/observe the TAK clients
        snap = relay.status()       # peers + per-type event counts
        relay.stop()

    Bind ``host="0.0.0.0"`` so both an emulator (via ``10.0.2.2``) and a
    LAN/USB-reverse physical device can reach the same relay.
    """

    outdir: str | Path
    host: str = "0.0.0.0"
    port: int = 8087

    _server: socketserver.ThreadingTCPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _peers: dict[int, _Peer] = field(default_factory=dict, init=False, repr=False)
    _manifest_fp: TextIO | None = field(default=None, init=False, repr=False)

    seq: int = field(default=0, init=False)
    type_counts: dict[str, int] = field(default_factory=dict, init=False)
    started_at: str = field(default="", init=False)

    def start(self) -> int:
        """Bind, begin accepting, and open the capture files. Returns the port."""
        out = Path(self.outdir)
        out.mkdir(parents=True, exist_ok=True)
        # Continue an existing capture dir past its highest sequence number — the
        # MAX, not the count, so a gap (a deleted middle file) never reuses a
        # number and overwrites a surviving capture.
        self.seq = _highest_seq(out)
        self._manifest_fp = (out / "manifest.jsonl").open("a", encoding="utf-8")
        self.started_at = _utc_now()

        relay = self
        socketserver.ThreadingTCPServer.allow_reuse_address = True

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                relay._handle(self.request, self.client_address[0])

        try:
            self._server = socketserver.ThreadingTCPServer((self.host, self.port), _Handler)
        except OSError:
            # Bind failed (port in use) — close the manifest we just opened so the
            # handle doesn't leak, then let the caller surface a structured error.
            if self._manifest_fp is not None:
                self._manifest_fp.close()
                self._manifest_fp = None
            raise
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            # ThreadingTCPServer.server_close() joins the non-daemon handler
            # threads, but shutdown() does not unblock one parked in sock.recv().
            # Close every peer socket first so those recv()s return and the
            # handlers exit — otherwise stop() hangs until a client disconnects.
            with self._lock:
                for p in list(self._peers.values()):
                    with contextlib.suppress(OSError):
                        p.sock.shutdown(socket.SHUT_RDWR)
                    with contextlib.suppress(OSError):
                        p.sock.close()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        # Close the manifest under the lock so it can't race a handler thread's
        # in-flight write (both take _lock — see _save).
        with self._lock:
            if self._manifest_fp is not None:
                self._manifest_fp.close()
                self._manifest_fp = None

    def __enter__(self) -> CotRelay:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def status(self) -> dict[str, object]:
        """Live snapshot: connected peers, total events, per-type breakdown."""
        with self._lock:
            peers = [
                {"addr": p.addr, "callsign": p.callsign, "events": p.events}
                for p in sorted(self._peers.values(), key=lambda q: q.ident)
            ]
            type_counts = dict(self.type_counts)
        return {
            "running": self._server is not None,
            "host": self.host,
            "port": self.port,
            "outdir": str(self.outdir),
            "started_at": self.started_at,
            "peers": peers,
            "peer_count": len(peers),
            "events_captured": self.seq,
            "type_counts": type_counts,
        }

    # -- internals ----------------------------------------------------------
    def _handle(self, sock: socket.socket, addr: str) -> None:
        ident = id(sock)
        peer = _Peer(ident=ident, addr=addr, sock=sock)
        with self._lock:
            self._peers[ident] = peer
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_EVENT_BYTES and _EVENT_END not in buf:
                    # Runaway un-terminated event; drop the buffer, keep the link.
                    buf = b""
                    continue
                while _EVENT_END in buf:
                    end = buf.index(_EVENT_END) + len(_EVENT_END)
                    start = buf.find(_EVENT_START)
                    if start == -1 or start > end:
                        # `</event>` with no matching start (junk/partial) — skip it.
                        buf = buf[end:]
                        continue
                    raw = buf[start:end]
                    buf = buf[end:]
                    self._save(raw, peer)
                    self._broadcast(ident, raw)
        except OSError:
            pass
        finally:
            with self._lock:
                self._peers.pop(ident, None)

    def _broadcast(self, from_ident: int, raw: bytes) -> None:
        with self._lock:
            targets = [p for p in self._peers.values() if p.ident != from_ident]
        for p in targets:
            try:
                p.sock.sendall(raw)
            except OSError:
                with self._lock:
                    self._peers.pop(p.ident, None)

    def _save(self, raw: bytes, peer: _Peer) -> None:
        cot_type = _match(_TYPE_RE, raw, "unknown")
        callsign = next((m for m in (_match(r, raw, "") for r in _CALLSIGN_RES) if m), "")
        rec = {
            "seq": 0,
            "time": _utc_now(),
            "peer": peer.addr,
            "type": cot_type,
            "callsign": callsign,
            "bytes": len(raw),
        }
        # Everything that touches shared state or the manifest happens under the
        # lock: sequence allocation, counters, and the manifest append. Keeping
        # the append here (not after releasing the lock) is what lets stop() close
        # the handle safely — a concurrent write can't race the close.
        with self._lock:
            self.seq += 1
            n = rec["seq"] = self.seq
            self.type_counts[cot_type] = self.type_counts.get(cot_type, 0) + 1
            peer.events += 1
            if callsign:  # pings carry none; keep the last real one for the status view
                peer.callsign = callsign
            if self._manifest_fp is not None:
                self._manifest_fp.write(json.dumps(rec) + "\n")
                self._manifest_fp.flush()
        # The per-event XML is a unique filename, so it needs no lock — write it
        # outside the critical section to keep the lock hold short.
        safe_type = cot_type.replace("/", "_")
        (Path(self.outdir) / f"{n:04d}_{safe_type}.xml").write_bytes(raw)


def _highest_seq(outdir: Path) -> int:
    """The largest ``NNNN_`` prefix among existing captures (0 if none)."""
    highest = 0
    for p in outdir.glob("*.xml"):
        m = _SEQ_RE.match(p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def _match(pattern: re.Pattern[bytes], raw: bytes, default: str) -> str:
    m = pattern.search(raw)
    return m.group(1).decode("utf-8", "replace") if m else default


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
