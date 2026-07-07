# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Standalone CoT streaming TAK server — point ATAK/iTAK/WinTAK at the sim.

A Meshtastic app (≥2.8) bridges mesh TAK traffic to a connected TAK client by
running an in-app TAK server that streams Cursor-on-Target (CoT) XML. This
module stands up that *same CoT stream* directly from a synthetic capture's TAK
squad — decompressing each TAKPacketV2 (portnum 78) and rebuilding its CoT via
the SDK — so a TAK client can connect straight to the simulator and render the
squad on its map, with no Meshtastic app or radio in the loop. It's the
app-plane counterpart to :mod:`~meshtastic_mcp.replay.engine` (which serves the
Meshtastic app), and the server the Android ATAK e2e loop
(``scripts/ci_atak_app_loop.py``) drives.

Wire: the classic TAK **TCP streaming** protocol — complete ``<event>…</event>``
CoT documents concatenated on the socket (no XML declaration, no length prefix),
which ATAK ingests as a plain streaming TCP input. Requires the ``[tak]`` extra
(meshtastic-tak) for the CoT build; :func:`capture_to_cot_events` raises without
it.
"""

from __future__ import annotations

import contextlib
import re
import socket
import threading
import time
from dataclasses import dataclass, field

from meshtastic.protobuf import mesh_pb2

from . import tak
from .capture import Capture

TAK_V2_PORT = 78
_XML_DECL_RE = re.compile(rb"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)


def _strip_decl(xml: str) -> bytes:
    """Drop the ``<?xml?>`` declaration — CoT stream events are bare elements."""
    return _XML_DECL_RE.sub(b"", xml.encode("utf-8")).strip() + b"\n"


def capture_to_cot_events(cap: Capture) -> list[tuple[int, bytes]]:
    """``(rx_time, cot_xml_bytes)`` for every TAKPacketV2 in the capture.

    Reproduces the app TAK server's receive path (wire → TAKPacketV2 → CoT XML)
    for each portnum-78 packet, in time order. Legacy v1 (portnum 72) is skipped
    — generate the squad with ``profile={"tak": {"wire": "v2", ...}}``.
    """
    tak._require()
    from meshtastic_tak import CotXmlBuilder, TakCompressor

    comp = TakCompressor()
    builder = CotXmlBuilder()
    out: list[tuple[int, bytes]] = []
    mp = mesh_pb2.MeshPacket()
    for rxt, raw, _ch in cap.packets:
        mp.Clear()
        try:
            mp.ParseFromString(raw)
        except Exception:
            continue
        if mp.WhichOneof("payload_variant") != "decoded" or mp.decoded.portnum != TAK_V2_PORT:
            continue
        try:
            pkt = comp.decompress(mp.decoded.payload)
            out.append((rxt, _strip_decl(builder.build(pkt))))
        except Exception:
            continue
    return out


@dataclass
class CotTakServer:
    """Threaded CoT streaming server. Restamps event times to now and paces
    delivery to preserve the capture's cadence (capped by ``max_gap``).

    Usage::

        srv = CotTakServer(events, host="0.0.0.0", port=8087, speed=30, loop=True)
        srv.start()          # background thread; ATAK connects to host:port
        ...                  # drive/observe the TAK client
        srv.stop()
    """

    events: list[tuple[int, bytes]]
    host: str = "0.0.0.0"
    port: int = 8087
    speed: float = 1.0
    max_gap: float = 5.0
    loop: bool = False

    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    clients_served: int = field(default=0, init=False)
    events_sent: int = field(default=0, init=False)
    # inbound (TAK client -> mesh): raw CoT <event> documents the client sent
    received_cot: list[bytes] = field(default_factory=list, init=False)

    def start(self) -> int:
        """Bind + accept in the background. Returns the bound port."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> CotTakServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.clients_served += 1
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with client:
            client.settimeout(0.5)
            # inbound reader: a connected ATAK/iTAK sends its own CoT (markers,
            # GeoChat, self PLI) over the same stream — collect complete events.
            reader = threading.Thread(target=self._read_client, args=(client,), daemon=True)
            reader.start()
            while not self._stop.is_set():
                prev: int | None = None
                for rxt, cot in self.events:
                    if self._stop.is_set():
                        return
                    if prev is not None:
                        delay = min((rxt - prev) / max(self.speed, 1e-6), self.max_gap)
                        if delay > 0 and self._stop.wait(delay):
                            return
                    prev = rxt
                    # restamp time/start/stale to now so the client treats it live
                    payload = _restamp(cot)
                    try:
                        client.sendall(payload)
                    except OSError:
                        return
                    self.events_sent += 1
                if not self.loop:
                    # keep the socket open a moment so a late inbound event lands
                    self._stop.wait(0.5)
                    return

    def _read_client(self, client: socket.socket) -> None:
        """Accumulate the client's stream and split out complete CoT events."""
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = client.recv(4096)
            except (TimeoutError, OSError):
                if self._stop.is_set():
                    return
                continue
            if not chunk:
                return
            buf += chunk
            while b"</event>" in buf:
                end = buf.index(b"</event>") + len(b"</event>")
                start = buf.find(b"<event")
                if start == -1 or start > end:
                    buf = buf[end:]
                    continue
                self.received_cot.append(buf[start:end])
                buf = buf[end:]


_TIME_ATTR_RE = re.compile(rb'(time|start)="[^"]*"')
_STALE_ATTR_RE = re.compile(rb'stale="[^"]*"')


def _restamp(cot: bytes) -> bytes:
    """Rewrite time/start to now and stale to now+2min (client liveness)."""
    now = time.gmtime()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", now).encode()
    stale = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 120)).encode()
    cot = _TIME_ATTR_RE.sub(lambda m: m.group(1) + b'="' + ts + b'"', cot)
    cot = _STALE_ATTR_RE.sub(b'stale="' + stale + b'"', cot)
    return cot
