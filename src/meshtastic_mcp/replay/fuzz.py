# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fuzzing / adversary layer for the replay engine.

Turns the deterministic replay stream into a hostile-mesh simulator. Two
families of fault, both seeded (so a crash reproduces):

  **Protocol fuzzing** (parser / decoder robustness) — mutate packets on the
  wire: corrupt or truncate the decoded payload, replace it with garbage, set a
  portnum that disagrees with the body, inject invalid UTF-8 into text, push
  impossible telemetry / position / hop values, duplicate (replay) or drop.

  **Bad-actor campaigns** (semantic / security) — inject *new* packets from
  adversary identities: an "evil twin" impersonating a real node, a flooder
  hammering a channel, a GPS spoofer teleporting around the globe, forged
  routing ACKs, and rogue ADMIN packets (reboot / factory-reset requests) aimed
  at the connected app.

The :class:`Fuzzer` is driven by a :class:`FuzzConfig` (per-fault rates +
campaign params). The engine calls :meth:`on_packet` for every streamed packet
and :meth:`on_tick` once per loop iteration for time-based campaigns. Every
action is recorded to a bounded event log surfaced in ``replay_status``.

Presets: ``off``, ``light``, ``parser``, ``adversary``, ``chaos`` — see
:func:`preset`.
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import admin_pb2, mesh_pb2, telemetry_pb2

if TYPE_CHECKING:
    from .capture import NodeRow

BROADCAST = 0xFFFFFFFF
# AdminMessage observer target (the node the app connects "as").
OBSERVER_NUM = 0x42524331


@dataclass
class FuzzConfig:
    """Per-fault rates (0..1 unless noted) + campaign parameters.

    Rates in ``on_packet`` are evaluated per streamed packet; a packet may be
    hit by at most one *mutating* fault (checked in a fixed order) plus the
    independent drop/duplicate dice.
    """

    seed: int = 0
    name: str = "custom"

    # -- protocol fuzzing (per-packet) --
    corrupt_payload: float = 0.0  # bit-flip / truncate / append junk in decoded.payload
    garbage_payload: float = 0.0  # replace decoded.payload with random bytes
    portnum_mismatch: float = 0.0  # keep payload, swap portnum to a different one
    bad_text: float = 0.0  # invalid UTF-8 / control chars / huge string (text pkts)
    impossible_telemetry: float = 0.0  # battery 250%, negative voltage, chutil 9999 (telem pkts)
    teleport_position: float = 0.0  # out-of-range lat/lon, impossible speed/alt (pos pkts)
    hop_anomaly: float = 0.0  # hop_limit > hop_start, hop_start absurd
    spoof_sender: float = 0.0  # rewrite `from` to impersonate another real node
    duplicate: float = 0.0  # re-emit the packet (replay attack / dup)
    drop: float = 0.0  # silently drop the packet (loss)

    # -- transport fuzzing (stream framing) --
    corrupt_frame: float = 0.0  # mangle the outgoing frame bytes (can desync the app)

    # -- bad-actor campaigns (time-based, packets/sec or per-interval) --
    evil_twin: bool = False  # impersonate a real node's identity (flapping NodeInfo)
    evil_twin_interval: float = 20.0
    flooder: bool = False  # burst broadcast traffic from one adversary node
    flooder_rate: float = 8.0  # packets/sec while flooding
    gps_spoofer: bool = False  # adversary node teleporting worldwide
    gps_spoofer_interval: float = 5.0
    forged_acks: bool = False  # spoofed routing ACKs from random nodes
    forged_acks_interval: float = 7.0
    rogue_admin: bool = False  # unauthorized ADMIN packets to the observer
    rogue_admin_interval: float = 30.0
    waypoint_spam: bool = False  # oversized / malicious waypoints
    waypoint_spam_interval: float = 25.0
    ninja_flood: bool = False  # DC33-style mass NodeInfo display-name spoof
    ninja_flood_interval: float = 12.0
    ninja_flood_batch: int = 6  # real nodes whose names get overwritten per fire


@dataclass
class _Stats:
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    counts: dict[str, int] = field(default_factory=dict)

    def log(self, kind: str, detail: str = "", idx: int | None = None) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.events.append({"t": round(time.time(), 2), "kind": kind, "detail": detail, "idx": idx})


# Portnums a mismatch can target (real, decodable app ports).
_PORTNUM_POOL = [1, 3, 4, 5, 6, 8, 34, 65, 67, 70, 71]


class Fuzzer:
    """Stateful, seeded fault injector applied to the replay stream."""

    def __init__(self, config: FuzzConfig, nodes: list[NodeRow], ch_index: dict[str, int]):
        self.cfg = config
        self.rng = random.Random(config.seed)
        self.nodes = nodes
        self.ch_index = ch_index
        self.channels = list(ch_index)
        self.stats = _Stats()
        self._idx = 0
        self._next: dict[str, float] = {}  # campaign -> next-fire wall clock
        self._flood_started: float | None = None

        node_nums = [n.num for n in nodes] or [self.rng.randint(0x10000000, 0xEFFFFFFF)]
        self._node_nums = node_nums
        # adversary identities
        self._twin_target = self.rng.choice(nodes) if nodes else None
        self._flooder_num = self.rng.randint(0x10000000, 0xEFFFFFFF)
        self._gps_num = self.rng.randint(0x10000000, 0xEFFFFFFF)

    # ── per-packet hook ──────────────────────────────────────────────────────
    def on_packet(self, mp: mesh_pb2.MeshPacket, ch_name: str) -> list[mesh_pb2.MeshPacket]:
        """Return the packet(s) to actually send (0 = dropped, >1 = duplicated)."""
        self._idx += 1
        c = self.cfg
        r = self.rng

        # loss
        if c.drop and r.random() < c.drop:
            self.stats.log("drop", idx=self._idx)
            return []

        # at most one mutating fault, in priority order
        pn = mp.decoded.portnum
        applied = None
        if c.garbage_payload and r.random() < c.garbage_payload:
            mp.decoded.payload = bytes(r.randint(0, 255) for _ in range(r.randint(0, 64)))
            applied = "garbage_payload"
        elif c.corrupt_payload and r.random() < c.corrupt_payload:
            applied = self._corrupt_payload(mp)
        elif c.portnum_mismatch and r.random() < c.portnum_mismatch:
            new = r.choice([p for p in _PORTNUM_POOL if p != pn] or _PORTNUM_POOL)
            mp.decoded.portnum = new
            applied = f"portnum_mismatch:{pn}->{new}"
        elif c.bad_text and pn == 1 and r.random() < c.bad_text:
            applied = self._bad_text(mp)
        elif c.impossible_telemetry and pn == 67 and r.random() < c.impossible_telemetry:
            applied = self._impossible_telemetry(mp)
        elif c.teleport_position and pn == 3 and r.random() < c.teleport_position:
            applied = self._teleport(mp)
        if applied:
            self.stats.log(applied.split(":")[0], applied, idx=self._idx)

        # hop anomaly is independent of the body mutation
        if c.hop_anomaly and r.random() < c.hop_anomaly:
            mp.hop_start = r.choice([0, 200, 250])
            mp.hop_limit = min(7, mp.hop_start + r.randint(1, 5)) if mp.hop_start < 8 else 7
            self.stats.log("hop_anomaly", f"start={mp.hop_start} limit={mp.hop_limit}", self._idx)

        # sender spoofing
        if c.spoof_sender and self.nodes and r.random() < c.spoof_sender:
            victim = r.choice(self.nodes).num
            setattr(mp, "from", victim & 0xFFFFFFFF)
            self.stats.log("spoof_sender", f"as=!{victim:08x}", self._idx)

        out = [mp]
        # duplicate / replay
        if c.duplicate and r.random() < c.duplicate:
            dup = mesh_pb2.MeshPacket()
            dup.CopyFrom(mp)
            out.append(dup)
            self.stats.log("duplicate", f"id={mp.id}", self._idx)
        return out

    # ── time-based campaigns ─────────────────────────────────────────────────
    def on_tick(self, now: float) -> list[mesh_pb2.MeshPacket]:
        out: list[mesh_pb2.MeshPacket] = []
        c = self.cfg
        if c.flooder:
            out.extend(self._flood(now))
        if c.gps_spoofer and self._due("gps", now, c.gps_spoofer_interval):
            out.append(self._gps_spoof())
        if c.evil_twin and self._twin_target and self._due("twin", now, c.evil_twin_interval):
            out.append(self._evil_twin())
        if c.forged_acks and self._due("ack", now, c.forged_acks_interval):
            out.append(self._forged_ack())
        if c.rogue_admin and self._due("admin", now, c.rogue_admin_interval):
            out.append(self._rogue_admin())
        if c.waypoint_spam and self._due("wpt", now, c.waypoint_spam_interval):
            out.append(self._waypoint_spam())
        if c.ninja_flood and self.nodes and self._due("ninja", now, c.ninja_flood_interval):
            out.extend(self._ninja_flood())
        return out

    # ── transport hook ───────────────────────────────────────────────────────
    def maybe_corrupt_frame(self, data: bytes) -> bytes:
        if self.cfg.corrupt_frame and self.rng.random() < self.cfg.corrupt_frame:
            b = bytearray(data)
            # flip a byte somewhere after the 4-byte header, or lie about length
            if len(b) > 4 and self.rng.random() < 0.5:
                i = self.rng.randint(4, len(b) - 1)
                b[i] ^= 1 << self.rng.randint(0, 7)
                self.stats.log("corrupt_frame", f"flip@{i}")
            else:
                b[2] = self.rng.randint(0, 255)
                b[3] = self.rng.randint(0, 255)
                self.stats.log("corrupt_frame", "len-prefix")
            return bytes(b)
        return data

    # ── mutators ─────────────────────────────────────────────────────────────
    def _corrupt_payload(self, mp: mesh_pb2.MeshPacket) -> str:
        b = bytearray(mp.decoded.payload)
        if not b:
            mp.decoded.payload = bytes(self.rng.randint(0, 255) for _ in range(4))
            return "corrupt_payload:empty->junk"
        mode = self.rng.choice(["flip", "truncate", "append"])
        if mode == "flip":
            i = self.rng.randrange(len(b))
            b[i] ^= 1 << self.rng.randint(0, 7)
        elif mode == "truncate":
            b = b[: self.rng.randint(0, max(0, len(b) - 1))]
        else:
            b += bytes(self.rng.randint(0, 255) for _ in range(self.rng.randint(1, 32)))
        mp.decoded.payload = bytes(b)
        return f"corrupt_payload:{mode}"

    def _bad_text(self, mp: mesh_pb2.MeshPacket) -> str:
        choice = self.rng.choice(["invalid_utf8", "control", "huge", "nullbytes"])
        if choice == "invalid_utf8":
            mp.decoded.payload = b"\xff\xfe\x80\x81 bad utf8 \xc3\x28"
        elif choice == "control":
            mp.decoded.payload = b"\x00\x07\x1b[31m ansi \x08\x7f"
        elif choice == "huge":
            mp.decoded.payload = b"A" * 4096
        else:
            mp.decoded.payload = b"null\x00in\x00middle"
        return f"bad_text:{choice}"

    def _impossible_telemetry(self, mp: mesh_pb2.MeshPacket) -> str:
        tm = telemetry_pb2.Telemetry()
        try:
            tm.ParseFromString(mp.decoded.payload)
        except Exception:
            pass
        d = tm.device_metrics
        d.battery_level = self.rng.choice([200, 250, 4_000_000_000])
        d.voltage = self.rng.choice([-5.0, 999.0, 1e9])
        d.channel_utilization = 9999.0
        d.air_util_tx = -1.0
        mp.decoded.payload = tm.SerializeToString()
        return "impossible_telemetry"

    def _teleport(self, mp: mesh_pb2.MeshPacket) -> str:
        p = mesh_pb2.Position()
        try:
            p.ParseFromString(mp.decoded.payload)
        except Exception:
            pass
        kind = self.rng.choice(["oob", "null_island", "global_jump", "speed"])
        if kind == "oob":
            p.latitude_i = self.rng.choice([2_000_000_000, -2_000_000_000])
            p.longitude_i = self.rng.choice([2_000_000_000, -2_000_000_000])
        elif kind == "null_island":
            p.latitude_i = 0
            p.longitude_i = 0
        elif kind == "global_jump":
            p.latitude_i = self.rng.randint(-900_000_000, 900_000_000)
            p.longitude_i = self.rng.randint(-1_800_000_000, 1_800_000_000)
        else:
            p.ground_speed = self.rng.choice([5000, 4_000_000_000])
        p.altitude = self.rng.choice([-100000, 999999])
        mp.decoded.payload = p.SerializeToString()
        return f"teleport_position:{kind}"

    # ── campaign builders ────────────────────────────────────────────────────
    def _new(
        self, frm: int, to: int, portnum: int, payload: bytes, ch: int = 0, hop_limit: int = 3
    ) -> mesh_pb2.MeshPacket:
        mp = mesh_pb2.MeshPacket()
        setattr(mp, "from", frm & 0xFFFFFFFF)
        mp.to = to & 0xFFFFFFFF
        mp.id = self.rng.randint(1, 0x7FFFFFFF)
        mp.rx_time = int(time.time())
        mp.hop_limit = hop_limit
        mp.hop_start = hop_limit
        mp.channel = ch
        mp.decoded.portnum = portnum
        mp.decoded.payload = payload
        return mp

    def _flood(self, now: float) -> list[mesh_pb2.MeshPacket]:
        # burst at flooder_rate for a windowed period, then idle, repeat
        if self._flood_started is None:
            self._flood_started = now
            self._next["flood"] = now
        period = 1.0 / max(self.cfg.flooder_rate, 0.1)
        out = []
        while self._next.get("flood", now) <= now:
            txt = f"FLOOD {self.rng.randint(0, 1 << 30)} ".encode() * self.rng.randint(1, 6)
            out.append(self._new(self._flooder_num, BROADCAST, 1, txt))
            self._next["flood"] = self._next.get("flood", now) + period
            if len(out) > 50:  # safety cap per tick
                break
        if out:
            self.stats.log("flooder", f"n={len(out)}")
        return out

    def _gps_spoof(self) -> mesh_pb2.MeshPacket:
        p = mesh_pb2.Position()
        p.latitude_i = self.rng.randint(-900_000_000, 900_000_000)
        p.longitude_i = self.rng.randint(-1_800_000_000, 1_800_000_000)
        p.altitude = self.rng.randint(-500, 30000)
        p.time = int(time.time())
        p.ground_speed = self.rng.randint(0, 6000)
        self.stats.log("gps_spoofer", f"!{self._gps_num:08x}")
        return self._new(self._gps_num, BROADCAST, 3, p.SerializeToString())

    def _evil_twin(self) -> mesh_pb2.MeshPacket:
        assert self._twin_target is not None
        t = self._twin_target
        u = mesh_pb2.User()
        u.id = t.node_id
        u.long_name = (t.long_name or "node") + self.rng.choice([" ", "  ", "!", " \u200b"])
        u.short_name = (t.short_name or "EVIL")[:4]
        u.public_key = bytes(self.rng.randint(0, 255) for _ in range(32))  # different key = MITM
        self.stats.log("evil_twin", f"imp=!{t.num:08x}")
        return self._new(t.num, BROADCAST, 4, u.SerializeToString())

    def _forged_ack(self) -> mesh_pb2.MeshPacket:
        r = mesh_pb2.Routing()
        r.error_reason = mesh_pb2.Routing.Error.NONE
        frm = self.rng.choice(self._node_nums)
        to = self.rng.choice(self._node_nums)
        self.stats.log("forged_acks", f"!{frm:08x}->!{to:08x}")
        return self._new(frm, to, 5, r.SerializeToString(), hop_limit=0)

    def _rogue_admin(self) -> mesh_pb2.MeshPacket:
        a = admin_pb2.AdminMessage()
        which = self.rng.choice(["reboot", "factory_reset", "set_owner"])
        if which == "reboot":
            a.reboot_seconds = 5
        elif which == "factory_reset":
            a.factory_reset_device = 1
        else:
            a.set_owner.long_name = "PWNED"
            a.set_owner.short_name = "PWND"
        frm = self.rng.choice(self._node_nums)
        self.stats.log("rogue_admin", f"{which} from !{frm:08x}")
        return self._new(frm, OBSERVER_NUM, 6, a.SerializeToString())

    def _ninja_flood(self) -> list[mesh_pb2.MeshPacket]:
        """Replayed NodeInfo that overwrites real nodes' display names (the DC33
        🥷 attack). Uses each victim's *real* node num and omits public_key, so
        (unlike evil_twin's key swap) the mobile client's key-change warning is
        NOT triggered — the spoof silently corrupts everyone's node DB."""
        batch = min(self.cfg.ninja_flood_batch, len(self.nodes))
        out: list[mesh_pb2.MeshPacket] = []
        for victim in self.rng.sample(self.nodes, batch):
            u = mesh_pb2.User()
            u.id = victim.node_id
            base = victim.long_name or "node"
            u.long_name = self.rng.choice([f"{base} 🥷", f"🥷 {base}", "🥷🥷🥷"])
            u.short_name = self.rng.choice(["🥷", (victim.short_name or "ninj")[:4]])
            # deliberately NO public_key -> presents as "same key", no warning
            out.append(self._new(victim.num, BROADCAST, 4, u.SerializeToString(), hop_limit=3))
        self.stats.log("ninja_flood", f"n={batch}")
        return out

    def _waypoint_spam(self) -> mesh_pb2.MeshPacket:
        w = mesh_pb2.Waypoint()
        w.id = self.rng.randint(1, 0x7FFFFFFF)
        w.latitude_i = self.rng.randint(-900_000_000, 900_000_000)
        w.longitude_i = self.rng.randint(-1_800_000_000, 1_800_000_000)
        w.name = "X" * 60
        w.description = self.rng.choice(
            [
                "A" * 1000,
                "<script>alert(1)</script>",
                "'; DROP TABLE nodes;--",
                "\x00\x1b[2J malicious \u202e",
            ]
        )
        w.icon = self.rng.randint(0, 0x10FFFF)
        frm = self.rng.choice(self._node_nums)
        self.stats.log("waypoint_spam", f"!{frm:08x}")
        return self._new(frm, BROADCAST, 8, w.SerializeToString(), hop_limit=4)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _due(self, key: str, now: float, interval: float) -> bool:
        nxt = self._next.get(key)
        if nxt is None:
            # stagger first fire a little so campaigns don't all fire at t0
            self._next[key] = now + self.rng.uniform(0, interval)
            return False
        if now >= nxt:
            self._next[key] = now + interval * self.rng.uniform(0.7, 1.3)
            return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            "profile": self.cfg.name,
            "seed": self.cfg.seed,
            "counts": dict(self.stats.counts),
            "recent": list(self.stats.events)[-15:],
        }


# ── Presets ──────────────────────────────────────────────────────────────────
def preset(name: str, seed: int = 0) -> FuzzConfig:
    """Named fuzz profiles. ``off`` returns a no-op config."""
    name = (name or "off").lower()
    if name == "off":
        return FuzzConfig(seed=seed, name="off")
    if name == "light":
        # rare corruption + realistic loss/dup — good for soak / robustness
        return FuzzConfig(
            seed=seed,
            name="light",
            corrupt_payload=0.01,
            drop=0.02,
            duplicate=0.01,
            hop_anomaly=0.005,
        )
    if name == "parser":
        # hammer the decoder paths — malformed bodies, mismatches, bad text/values
        return FuzzConfig(
            seed=seed,
            name="parser",
            corrupt_payload=0.20,
            garbage_payload=0.10,
            portnum_mismatch=0.10,
            bad_text=0.5,
            impossible_telemetry=0.4,
            teleport_position=0.3,
            hop_anomaly=0.05,
            duplicate=0.03,
        )
    if name == "ninja":
        # DC33 NodeInfo display-name spoofing campaign (name corruption at scale)
        return FuzzConfig(
            seed=seed,
            name="ninja",
            ninja_flood=True,
            ninja_flood_interval=10.0,
            ninja_flood_batch=8,
        )
    if name == "adversary":
        # bad-actor campaigns; light protocol noise
        return FuzzConfig(
            seed=seed,
            name="adversary",
            spoof_sender=0.03,
            corrupt_payload=0.02,
            evil_twin=True,
            flooder=True,
            flooder_rate=6.0,
            gps_spoofer=True,
            forged_acks=True,
            rogue_admin=True,
            waypoint_spam=True,
            ninja_flood=True,
        )
    if name == "chaos":
        # everything cranked, including transport-level frame corruption
        return FuzzConfig(
            seed=seed,
            name="chaos",
            corrupt_payload=0.25,
            garbage_payload=0.15,
            portnum_mismatch=0.15,
            bad_text=0.6,
            impossible_telemetry=0.6,
            teleport_position=0.5,
            hop_anomaly=0.1,
            spoof_sender=0.1,
            duplicate=0.05,
            drop=0.03,
            corrupt_frame=0.01,
            evil_twin=True,
            flooder=True,
            flooder_rate=12.0,
            gps_spoofer=True,
            forged_acks=True,
            rogue_admin=True,
            waypoint_spam=True,
            ninja_flood=True,
        )
    raise ValueError(f"unknown fuzz preset: {name!r}")


PRESET_NAMES = ["off", "light", "parser", "ninja", "adversary", "chaos"]


def from_spec(spec: str | dict | None, seed: int = 0) -> FuzzConfig | None:
    """Resolve a tool-facing fuzz spec into a FuzzConfig (or None = disabled).

    ``spec`` may be a preset name, a dict of FuzzConfig overrides (optionally
    with a ``preset`` base), or None.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        cfg = preset(spec, seed=seed)
        return None if cfg.name == "off" else cfg
    if isinstance(spec, dict):
        base = preset(str(spec.get("preset", "off")), seed=seed)
        valid = base.__dataclass_fields__
        for k, v in spec.items():
            if k in valid and k != "preset":
                setattr(base, k, v)
        base.seed = int(spec.get("seed", seed))
        if "name" not in spec:
            base.name = str(spec.get("preset", base.name)) + "+custom"
        return base
    raise TypeError(f"unsupported fuzz spec: {type(spec).__name__}")
