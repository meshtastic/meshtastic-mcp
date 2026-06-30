# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Replay engine — a simulated Meshtastic TCP device.

The inverse of the recorder: instead of subscribing to a live mesh and writing
packets out, this *serves* a :class:`~meshtastic_mcp.replay.capture.Capture` as
a TCP device. An app (or the meshtastic Python lib) connects to the listen port,
performs the standard want-config handshake, and then receives a paced stream of
the captured packets — restamped to "now" — exactly as if a radio were sitting
in the mesh.

This is a from-scratch implementation of the Meshtastic stream protocol
(``0x94 0xc3`` + big-endian uint16 length + protobuf) and the two-phase
want-config handshake. It shares nothing with any external replay tool; it only
reuses the project's bundled meshtastic protobufs.

Sessions run in background threads under a process-global :class:`ReplayManager`
so the MCP tools can ``start`` / ``status`` / ``stop`` them without blocking.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2

from .capture import Capture, node_to_nodeinfo
from .fuzz import FuzzConfig, Fuzzer

START1 = 0x94
START2 = 0xC3

# want-config nonces the app uses (config phase, then node-DB phase).
NONCE_CONFIG = 69420
NONCE_DB = 69421

# Synthetic observer node the app connects "as" (must not collide with capture).
OBSERVER_NUM = 0x42524331  # "BRC1"


class PortInUseError(RuntimeError):
    """Raised when the replay listen port is already taken (clear, actionable)."""


# Synthetic "Replay Clock" node that posts progress messages into the mesh.
ANNOUNCER_NUM = 0x5245504C  # "REPL"
TEXT_MESSAGE_APP = 1


def _format_eta(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"~{s // 60}m {s % 60}s left" if s >= 60 else f"~{s}s left"


def local_ips() -> list[str]:
    """Best-effort list of this host's IPs (so a user knows where to point an app)."""
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# ── Frame helpers ────────────────────────────────────────────────────────────
def _frame(payload: bytes) -> bytes:
    return bytes([START1, START2]) + struct.pack(">H", len(payload)) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_toradio(sock: socket.socket) -> mesh_pb2.ToRadio | None:
    """Block until one full ToRadio frame is read; None on EOF."""
    state = 0
    while True:
        b = sock.recv(1)
        if not b:
            return None
        byte = b[0]
        if state == 0 and byte == START1:
            state = 1
        elif state == 1 and byte == START2:
            break
        else:
            state = 1 if byte == START1 else 0
    hdr = _recv_exact(sock, 2)
    if hdr is None:
        return None
    (length,) = struct.unpack(">H", hdr)
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    tr = mesh_pb2.ToRadio()
    try:
        tr.ParseFromString(payload)
    except Exception:
        return mesh_pb2.ToRadio()
    return tr


# ── Session ──────────────────────────────────────────────────────────────────
@dataclass
class ReplayParams:
    host: str = "0.0.0.0"
    port: int = 4403
    speed: float = 1.0  # playback multiplier (preserves cadence)
    rate: float | None = None  # steady packets/sec (ignores capture timing)
    max_gap: float = 20.0  # cap idle seconds between packets
    start: int | None = None  # window start epoch
    end: int | None = None  # window end epoch
    loop: bool = False
    limit_nodes: int = 200
    node_delay: float = 0.01  # spacing between NodeInfos during DB download
    fuzz: FuzzConfig | None = None  # fault-injection / adversary layer (None = clean)
    # observer (connected node) position; None => derive from the capture center
    observer_lat: int | None = None
    observer_lon: int | None = None
    modem_preset: str = "LONG_FAST"  # advertised LoRa preset
    firmware_edition: str = "VANILLA"  # drives the app's event banner (e.g. DEFCON, HAMVENTION)
    # Replay Clock: post a kickoff + periodic "ETA — done/total" to the busiest
    # channel so you can see, from inside the app, that it's a replay. 0 = off.
    announce_interval: float = 0.0
    send_timeout: float = 10.0  # SO_SNDTIMEO seconds; a stalled app can't hang us. 0 = none


@dataclass
class _SessionState:
    id: str
    params: ReplayParams
    capture_label: str
    packets_total: int
    started_at: float
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    client_addr: str | None = None
    packets_sent: int = 0
    injected: int = 0
    connected: bool = False
    error: str | None = None
    ended: bool = False


class ReplaySession:
    """One TCP listener serving a capture to a single connected client."""

    def __init__(self, sid: str, capture: Capture, params: ReplayParams):
        window = capture.window(params.start, params.end)
        self.capture = capture
        self.params = params
        self.window = window
        self.state = _SessionState(
            id=sid,
            params=params,
            capture_label=capture.label,
            packets_total=len(window),
            started_at=time.time(),
        )
        self._srv: socket.socket | None = None
        self._ch_index = {name: i for i, name in enumerate(capture.channels)}
        # live-injection queue: (MeshPacket, channel_name, fuzz) drained per client
        self._inject_q: queue.Queue[tuple[mesh_pb2.MeshPacket, str, bool]] = queue.Queue()
        self.fuzzer: Fuzzer | None = (
            Fuzzer(params.fuzz, capture.nodes, self._ch_index) if params.fuzz else None
        )

    # -- lifecycle --
    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.params.host, self.params.port))
        except OSError as exc:
            srv.close()
            raise PortInUseError(
                f"cannot bind {self.params.host}:{self.params.port} ({exc}). "
                f"Another server is using it — stop it (replay_stop), or pass port=0 "
                f"to auto-pick a free port."
            ) from exc
        # port=0 -> OS picks a free port; surface the real one in status
        self.params.port = srv.getsockname()[1]
        srv.listen(1)
        srv.settimeout(1.0)
        self._srv = srv
        t = threading.Thread(target=self._accept_loop, name=f"replay-{self.state.id}", daemon=True)
        self.state.thread = t
        t.start()

    def stop(self) -> None:
        self.state.stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass

    # -- server loop --
    def _accept_loop(self) -> None:
        assert self._srv is not None
        try:
            while not self.state.stop.is_set():
                try:
                    client, addr = self._srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if self.params.send_timeout:
                    client.settimeout(self.params.send_timeout)
                self.state.client_addr = f"{addr[0]}:{addr[1]}"
                self.state.connected = True
                try:
                    self._serve_client(client)
                except (OSError, ConnectionError) as exc:
                    self.state.error = str(exc)
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass
                    self.state.connected = False
                if not self.params.loop:
                    break
        finally:
            try:
                self._srv.close()
            except OSError:
                pass
            self.state.ended = True

    def _serve_client(self, client: socket.socket) -> None:
        send_lock = threading.Lock()
        streaming = [False]

        def send(fr: mesh_pb2.FromRadio) -> None:
            data = _frame(fr.SerializeToString())
            if self.fuzzer is not None:
                data = self.fuzzer.maybe_corrupt_frame(data)
            with send_lock:
                client.sendall(data)

        # injector: drains replay_inject()'d packets onto the live connection,
        # through the same send path (and optional fuzz mutator) as the stream.
        threading.Thread(
            target=self._inject_loop,
            args=(send,),
            daemon=True,
            name=f"replay-inject-{self.state.id}",
        ).start()

        while not self.state.stop.is_set():
            tr = _read_toradio(client)
            if tr is None:
                return
            if tr.HasField("want_config_id"):
                nonce = tr.want_config_id
                if nonce == NONCE_DB:
                    self._send_db_phase(send, nonce)
                    if not streaming[0]:
                        streaming[0] = True
                        threading.Thread(
                            target=self._stream,
                            args=(send,),
                            daemon=True,
                            name=f"replay-stream-{self.state.id}",
                        ).start()
                else:
                    self._send_config_phase(send, nonce)
            # heartbeats / other ToRadio: drain and ignore

    # -- handshake phases --
    def _send_config_phase(self, send: Any, nonce: int) -> None:
        n_nodes = len(self.capture.nodes)
        fr = mesh_pb2.FromRadio()
        mi = fr.my_info
        mi.my_node_num = OBSERVER_NUM
        mi.reboot_count = 1
        mi.min_app_version = 30200
        mi.nodedb_count = n_nodes
        mi.device_id = struct.pack("<I", OBSERVER_NUM) * 4  # stable 16-byte id
        mi.pio_env = "replay"
        with contextlib.suppress(ValueError, KeyError):
            mi.firmware_edition = mesh_pb2.FirmwareEdition.Value(self.params.firmware_edition)
        send(fr)

        fr = mesh_pb2.FromRadio()
        md = fr.metadata
        md.firmware_version = "2.7.8"
        md.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        md.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
        send(fr)

        send(self._observer_nodeinfo())

        specs = self.capture.channel_specs
        for idx, name in enumerate(self.capture.channels):
            fr = mesh_pb2.FromRadio()
            fr.channel.index = idx
            if specs is not None and idx < len(specs):
                # real keys/roles — lets the app live-decrypt encrypted packets
                s = specs[idx]
                fr.channel.role = (
                    channel_pb2.Channel.Role.PRIMARY
                    if s.primary
                    else channel_pb2.Channel.Role.SECONDARY
                )
                fr.channel.settings.psk = s.psk
                fr.channel.settings.name = s.app_name
            elif idx == 0:
                fr.channel.role = channel_pb2.Channel.Role.PRIMARY
                fr.channel.settings.psk = bytes([0x01])
                fr.channel.settings.name = "" if name == "LongFast" else name
            else:
                fr.channel.role = channel_pb2.Channel.Role.SECONDARY
                fr.channel.settings.psk = bytes([(0x10 + idx) & 0xFF] * 16)
                fr.channel.settings.name = name
            send(fr)

        fr = mesh_pb2.FromRadio()
        fr.config.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        send(fr)
        fr = mesh_pb2.FromRadio()
        fr.config.lora.use_preset = True
        try:
            fr.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.Value(
                self.params.modem_preset
            )
        except (ValueError, KeyError):
            fr.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        fr.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        fr.config.lora.hop_limit = 3
        fr.config.lora.tx_enabled = False  # read-only replay
        send(fr)
        fr = mesh_pb2.FromRadio()
        fr.moduleConfig.mqtt.enabled = False
        send(fr)

        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = nonce
        send(fr)

    def _send_db_phase(self, send: Any, nonce: int) -> None:
        now = int(time.time())
        send(self._observer_nodeinfo())
        if self.params.node_delay > 0:
            time.sleep(self.params.node_delay)
        for n in self.capture.nodes:
            fr = mesh_pb2.FromRadio()
            fr.node_info.CopyFrom(node_to_nodeinfo(n, last_heard=now))
            send(fr)
            if self.params.node_delay > 0:
                time.sleep(self.params.node_delay)
        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = nonce
        send(fr)

    def _observer_nodeinfo(self) -> mesh_pb2.FromRadio:
        fr = mesh_pb2.FromRadio()
        ni = fr.node_info
        ni.num = OBSERVER_NUM
        ni.user.id = f"!{OBSERVER_NUM:08x}"
        ni.user.long_name = "Replay Observer"
        ni.user.short_name = "RPLY"
        ni.user.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
        ni.user.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        # "you are here": params override, else the capture's median position, so
        # the app map centers on the mesh and node distances are sensible.
        pos = None
        if self.params.observer_lat and self.params.observer_lon:
            pos = (self.params.observer_lat, self.params.observer_lon)
        else:
            pos = self.capture.center()
        if pos:
            ni.position.latitude_i, ni.position.longitude_i = pos
        ni.last_heard = int(time.time())
        ni.hops_away = 0
        return fr

    def _announcer_nodeinfo(self) -> mesh_pb2.FromRadio:
        fr = mesh_pb2.FromRadio()
        ni = fr.node_info
        ni.num = ANNOUNCER_NUM
        ni.user.id = f"!{ANNOUNCER_NUM:08x}"
        ni.user.long_name = "Replay Clock"
        ni.user.short_name = "TIME"
        ni.last_heard = int(time.time())
        ni.hops_away = 0
        return fr

    def _announce(self, send: Any, text: str, ch_idx: int) -> None:
        mp = mesh_pb2.MeshPacket()
        setattr(mp, "from", ANNOUNCER_NUM)
        mp.to = 0xFFFFFFFF
        mp.channel = ch_idx
        mp.id = int(time.time() * 1000) & 0x7FFFFFFF
        mp.rx_time = int(time.time())
        mp.hop_limit = 3
        mp.decoded.portnum = TEXT_MESSAGE_APP
        mp.decoded.payload = text.encode("utf-8")
        fr = mesh_pb2.FromRadio()
        fr.packet.CopyFrom(mp)
        send(fr)

    # -- live injection --
    def inject(
        self, packets: list[mesh_pb2.MeshPacket], *, channel: str = "LongFast", fuzz: bool = False
    ) -> int:
        """Queue MeshPackets to emit onto the live connection (sent promptly).

        Targeted counterpart to the fuzzer's random campaigns; with ``fuzz=True``
        each packet passes through the active fuzz mutator before send (inject a
        deliberately malformed packet). Packets queued before a client connects
        are delivered on connect.
        """
        for mp in packets:
            self._inject_q.put((mp, channel, fuzz))
        return len(packets)

    def _inject_loop(self, send: Any) -> None:
        stop = self.state.stop
        while not stop.is_set():
            try:
                mp, ch_name, fuzz = self._inject_q.get(timeout=0.5)
            except queue.Empty:
                continue
            mp.rx_time = int(time.time())
            mp.channel = self._ch_index.get(ch_name, 0)
            outs = (
                self.fuzzer.on_packet(mp, ch_name) if (fuzz and self.fuzzer is not None) else [mp]
            )
            for out_mp in outs:
                fr = mesh_pb2.FromRadio()
                fr.packet.CopyFrom(out_mp)
                try:
                    send(fr)
                except (OSError, ConnectionError):
                    return
                self.state.packets_sent += 1
                self.state.injected += 1

    # -- stream loop --
    def _stream(self, send: Any) -> None:
        p = self.params
        pkts = self.window
        if not pkts:
            return
        mp = mesh_pb2.MeshPacket()
        fixed_delay = (1.0 / p.rate) if p.rate else None
        stop = self.state.stop
        total = len(pkts)
        # Replay Clock: pick the busiest channel so progress lands where the
        # viewer is looking; introduce the node + post a kickoff.
        announce = p.announce_interval and p.announce_interval > 0
        ann_idx = 0
        if announce:
            from collections import Counter

            busiest = Counter(ch for _, _, ch in pkts).most_common(1)
            ann_idx = self._ch_index.get(busiest[0][0], 0) if busiest else 0
            try:
                send(self._announcer_nodeinfo())
                eta = (total / p.rate) if p.rate else None
                kick = f"🔁 Replay starting — {total} packets"
                if eta:
                    kick += f" — {_format_eta(eta)}"
                self._announce(send, kick, ann_idx)
            except (OSError, ConnectionError):
                stop.set()
                return
        while not stop.is_set():
            prev: int | None = None
            pass_start = time.time()
            last_ann = pass_start
            done = 0
            for rxt, raw, ch_name in pkts:
                if stop.is_set():
                    return
                if fixed_delay is not None:
                    if stop.wait(fixed_delay):
                        return
                elif prev is not None:
                    delay = min((rxt - prev) / p.speed, p.max_gap)
                    if delay > 0 and stop.wait(delay):
                        return
                prev = rxt
                mp.Clear()
                try:
                    mp.ParseFromString(raw)
                except Exception:
                    continue
                mp.rx_time = int(time.time())  # time-travel: stamp as now
                mp.channel = self._ch_index.get(ch_name, 0)
                outs = self.fuzzer.on_packet(mp, ch_name) if self.fuzzer else [mp]
                if self.fuzzer is not None:
                    outs = outs + self.fuzzer.on_tick(time.time())
                for out_mp in outs:
                    fr = mesh_pb2.FromRadio()
                    fr.packet.CopyFrom(out_mp)
                    try:
                        send(fr)
                    except (OSError, ConnectionError):
                        stop.set()
                        return
                    self.state.packets_sent += 1
                done += 1
                if announce:
                    now = time.time()
                    if now - last_ann >= p.announce_interval:
                        eta = (now - pass_start) * (total - done) / done if done else 0
                        try:
                            self._announce(
                                send, f"⏱️ {_format_eta(eta)} — {done}/{total} packets", ann_idx
                            )
                        except (OSError, ConnectionError):
                            stop.set()
                            return
                        last_ann = now
            if announce and not stop.is_set():
                with contextlib.suppress(OSError, ConnectionError):
                    self._announce(send, f"✅ Replay complete — {total} packets", ann_idx)
            if not p.loop:
                self.state.ended = True
                return


# ── Manager (process-global) ─────────────────────────────────────────────────
class ReplayManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ReplaySession] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def start(self, capture: Capture, params: ReplayParams) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            sid = f"replay-{self._counter}"
            sess = ReplaySession(sid, capture, params)
            sess.start()
            self._sessions[sid] = sess
        return self.status(sid)

    def status(self, sid: str | None = None) -> dict[str, Any]:
        with self._lock:
            if sid is not None:
                sess = self._sessions.get(sid)
                return _status_dict(sess) if sess else {"error": f"no session {sid}"}
            return {"sessions": [_status_dict(s) for s in self._sessions.values()]}

    def inject(
        self,
        sid: str,
        packets: list[mesh_pb2.MeshPacket],
        *,
        channel: str = "LongFast",
        fuzz: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            return {"error": f"no session {sid}"}
        n = sess.inject(packets, channel=channel, fuzz=fuzz)
        return {"id": sid, "queued": n, "connected": sess.state.connected}

    def stop(self, sid: str | None = None) -> dict[str, Any]:
        with self._lock:
            targets = (
                [self._sessions[sid]]
                if sid and sid in self._sessions
                else (list(self._sessions.values()) if sid is None else [])
            )
            for s in targets:
                s.stop()
            stopped = [s.state.id for s in targets]
            for s in targets:
                self._sessions.pop(s.state.id, None)
        return {"stopped": stopped}


def _status_dict(sess: ReplaySession) -> dict[str, Any]:
    st = sess.state
    p = st.params
    span = sess.capture.span
    return {
        "id": st.id,
        "capture": st.capture_label,
        "listen": f"{p.host}:{p.port}",
        "connect": [f"{ip}:{p.port}" for ip in local_ips()],  # where to point an app
        "mode": (f"steady {p.rate}/s" if p.rate else f"{p.speed}x"),
        "loop": p.loop,
        "nodes": len(sess.capture.nodes),
        "channels": sess.capture.channels,
        "packets_total": st.packets_total,
        "packets_sent": st.packets_sent,
        "injected": st.injected,
        "connected": st.connected,
        "client": st.client_addr,
        "ended": st.ended,
        "error": st.error,
        "capture_span": {"start": span[0], "end": span[1]},
        "uptime_s": round(time.time() - st.started_at, 1),
        "fuzz": sess.fuzzer.status() if sess.fuzzer is not None else None,
    }


_MANAGER: ReplayManager | None = None


def get_manager() -> ReplayManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ReplayManager()
    return _MANAGER
