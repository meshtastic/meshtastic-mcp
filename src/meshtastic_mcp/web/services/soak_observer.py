# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Persistent API observers for the nightly soak.

Why raw serial capture cannot work here: firmware's ``SerialConsole`` silences
plain-text console logging the moment any protobuf client speaks on the port
(``usingProtobufs`` in ``SerialConsole.cpp``, permanent until reboot). The soak
preflight, every test send, and screen keep-alive are all protobuf touches — so
a pyserial text reader goes blind on every device within seconds of soak start
(observed as nightly ``log_silence`` on every baked board, runs 7–15).

Instead the soak holds ONE meshtastic ``SerialInterface`` per fleet device for
the whole window and records what the firmware actually streams to a connected
API client:

- received mesh packets (``meshtastic.receive``) — including the soak's own
  test messages arriving on peer nodes, which makes delivery measurable;
- telemetry (``meshtastic.receive.telemetry``) — battery/channel-utilization
  points attributed to the *originating* node;
- ``LogRecord`` lines (``meshtastic.log.line``) from devices that have
  ``security.debug_log_api_enabled`` set;
- connection loss/reconnect, which is the crash/wedge signal (a panicking
  board drops its API session).

Holding the interfaces also *removes* the per-send and per-keepalive-touch
open/close churn that wedges nRF52 native-USB CDC — sends and keep-alive input
events ride the already-open connection instead (see ``send_text`` /
``send_input_event``).

Port discipline: each open device holds the process-wide
``registry.port_lock`` (so in-process ``connection.connect()`` callers fail
fast with the usual busy error) and the caller is expected to have claimed the
serial in ``PortLocks`` and suspended its raw serial monitor first.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meshtastic_mcp import connection, registry
from meshtastic_mcp.recorder import parsers

log = logging.getLogger("meshtastic_mcp.web.soak_observer")

# Reconnect policy for a device whose API session drops mid-soak.
RECONNECT_MAX_ATTEMPTS = 3
RECONNECT_SPACING_S = 60.0
CONNECT_TIMEOUT_S = 30.0

# Record shape matches the serial-monitor sink records the soak always wrote
# (ts/serial/port/line/level/tag/heap_free/uptime_s) plus:
#   kind: "log" | "packet" | "status"
#   from_node / portnum / text on packet records
OnRecord = Callable[[dict], None]
OnTelemetry = Callable[[dict], None]


@dataclass
class _Held:
    serial: str
    port: str
    iface: Any = None
    port_lock: threading.Lock | None = None
    lost: bool = False
    reconnect_attempts: int = 0
    last_reconnect_ts: float = 0.0
    gave_up: bool = False


@dataclass
class ObserverSummary:
    opened: list[str] = field(default_factory=list)
    open_failed: dict[str, str] = field(default_factory=dict)  # serial -> error
    dropped: list[str] = field(default_factory=list)  # lost and never recovered


def _node_id(num: Any) -> str:
    try:
        return f"!{int(num):08x}"
    except (TypeError, ValueError):
        return str(num)


class SoakObserver:
    """Owns the held interfaces and turns pubsub events into soak records.

    Threading: ``open_device`` / ``health_tick`` / ``close_all`` run on the
    event loop (blocking work pushed to threads); pubsub handlers fire on
    meshtastic reader threads and only do lock-free reads of ``_by_port`` plus
    calls into the thread-safe sinks — they never open or close anything.
    """

    def __init__(
        self,
        *,
        on_record: OnRecord,
        on_telemetry: OnTelemetry,
        hub=None,
        node_map: dict[int, str] | None = None,
    ) -> None:
        self.on_record = on_record
        self.on_telemetry = on_telemetry
        self.hub = hub
        # node_num -> serial, for attributing telemetry to the node it is ABOUT.
        self.node_map = dict(node_map or {})
        self._held: dict[str, _Held] = {}  # serial -> state
        self._by_port: dict[str, _Held] = {}
        self._pubsub_handlers: list[tuple[str, Callable[..., Any]]] = []
        self.summary = ObserverSummary()

    # -- lifecycle -----------------------------------------------------------

    async def open_device(self, serial: str, port: str) -> bool:
        """Open and hold an interface. Returns False (and records why) on
        failure — the caller decides whether that is preflight-fatal."""
        held = _Held(serial=serial, port=port)
        try:
            await asyncio.to_thread(self._open_blocking, held)
        except Exception as exc:
            self.summary.open_failed[serial] = str(exc)
            self._emit_status(held, f"— could not open API observer: {exc} —")
            return False
        self._held[serial] = held
        self._by_port[port] = held
        if not self._pubsub_handlers:
            self._wire_pubsub()
        self.summary.opened.append(serial)
        self._emit_status(held, "— API observer connected —")
        return True

    def _open_blocking(self, held: _Held) -> None:
        from meshtastic.serial_interface import (
            SerialInterface,  # type: ignore[import-untyped]
        )

        active = registry.active_session_for_port(held.port)
        if active is not None:
            raise connection.ConnectionError(
                f"port {held.port} is held by serial session {active.id}"
            )
        lock = registry.port_lock(held.port)
        if not lock.acquire(blocking=False):
            raise connection.ConnectionError(f"port {held.port} is busy")
        try:
            held.iface = SerialInterface(
                devPath=held.port,
                connectNow=True,
                noProto=False,
                timeout=int(CONNECT_TIMEOUT_S),
            )
        except Exception:
            lock.release()
            raise
        held.port_lock = lock

    async def close_all(self) -> None:
        self._unwire_pubsub()
        for held in list(self._held.values()):
            await asyncio.to_thread(self._close_blocking, held)
            if held.gave_up or held.lost:
                self.summary.dropped.append(held.serial)
        self._held.clear()
        self._by_port.clear()

    def _close_blocking(self, held: _Held) -> None:
        if held.iface is not None:
            connection.close_bounded(held.iface)
            held.iface = None
        if held.port_lock is not None:
            try:
                held.port_lock.release()
            except RuntimeError:
                pass
            held.port_lock = None

    # -- health / reconnect --------------------------------------------------

    async def health_tick(self) -> None:
        """Called from the soak loop. Reconnects lost devices with bounded,
        spaced attempts; a device that exhausts them is left dropped (its
        absence is itself the signal the analysis reports)."""
        now = time.monotonic()
        for held in self._held.values():
            if not held.lost or held.gave_up:
                continue
            if now - held.last_reconnect_ts < RECONNECT_SPACING_S:
                continue
            if held.reconnect_attempts >= RECONNECT_MAX_ATTEMPTS:
                held.gave_up = True
                self._emit_status(
                    held,
                    f"— giving up after {held.reconnect_attempts} reconnect attempts —",
                )
                continue
            held.reconnect_attempts += 1
            held.last_reconnect_ts = now
            self._emit_status(
                held,
                f"— reconnect attempt {held.reconnect_attempts}/{RECONNECT_MAX_ATTEMPTS} —",
            )
            try:
                await asyncio.to_thread(self._close_blocking, held)
                await asyncio.to_thread(self._open_blocking, held)
            except Exception as exc:
                self._emit_status(held, f"— reconnect failed: {exc} —")
                continue
            held.lost = False
            held.reconnect_attempts = 0
            self._emit_status(held, "— API observer reconnected —")

    # -- actions over held interfaces ---------------------------------------

    def is_held(self, serial: str) -> bool:
        held = self._held.get(serial)
        return held is not None and held.iface is not None and not held.lost

    async def send_text(self, serial: str, text: str, channel_index: int = 0) -> Any:
        """Broadcast a text message from a held device. Raises when the device
        is not held or its session is lost, so the caller records the failure."""
        held = self._held.get(serial)
        if held is None or held.iface is None or held.lost:
            raise connection.ConnectionError(f"{serial}: no live soak observer session")
        iface = held.iface

        def _send() -> Any:
            packet = iface.sendText(text, destinationId="^all", channelIndex=channel_index)
            return getattr(packet, "id", None)

        return await asyncio.to_thread(_send)

    async def send_input_event(self, serial: str, event: int | str) -> bool:
        """Keep-alive relay: inject an input event over the held interface.

        Returns True when this observer owns the device (event delivered, or
        the device is down and the event is meaningfully undeliverable), False
        when the serial is not ours — the caller falls back to its own path.
        """
        held = self._held.get(serial)
        if held is None:
            return False
        if held.iface is None or held.lost:
            return True  # ours, but down — swallow rather than fight the port
        from meshtastic.protobuf import admin_pb2  # type: ignore[import-untyped]

        from meshtastic_mcp.input_events import coerce_event_code

        iface = held.iface

        def _send() -> None:
            msg = admin_pb2.AdminMessage()
            msg.send_input_event.event_code = coerce_event_code(event)
            iface.localNode._sendAdmin(msg)

        try:
            await asyncio.to_thread(_send)
        except Exception as exc:
            log.debug("keepalive relay to %s failed: %s", serial, exc)
        return True

    def channel_and_region(self, serial: str) -> tuple[str | None, str | None]:
        """Primary-channel name and region from the held interface's config
        (fetched during the connect handshake) — the preflight check without
        another connect."""
        from meshtastic_mcp import info as mt_info

        held = self._held.get(serial)
        if held is None or held.iface is None:
            return None, None
        return (
            mt_info.primary_channel_name(held.iface),
            mt_info.region_name(held.iface),
        )

    # -- pubsub --------------------------------------------------------------

    def _wire_pubsub(self) -> None:
        from pubsub import pub  # type: ignore[import-untyped]

        bindings: list[tuple[str, Callable[..., Any]]] = [
            ("meshtastic.receive", self._on_receive),
            ("meshtastic.receive.telemetry", self._on_telemetry_packet),
            ("meshtastic.log.line", self._on_log_line),
            ("meshtastic.connection.lost", self._on_connection_lost),
        ]
        for topic, handler in bindings:
            try:
                pub.subscribe(handler, topic)
                self._pubsub_handlers.append((topic, handler))
            except Exception as exc:
                log.warning("soak observer failed to subscribe to %s: %s", topic, exc)

    def _unwire_pubsub(self) -> None:
        from pubsub import pub  # type: ignore[import-untyped]

        for topic, handler in self._pubsub_handlers:
            try:
                pub.unsubscribe(handler, topic)
            except Exception:
                pass
        self._pubsub_handlers.clear()

    def _resolve(self, interface: Any) -> _Held | None:
        port = parsers.interface_label(interface).get("port")
        if not port:
            return None
        return self._by_port.get(port)

    # Handlers run on meshtastic reader threads — they must never raise and
    # must not open/close anything.

    def _emit(self, held: _Held, rec: dict) -> None:
        try:
            self.on_record(rec)
        except Exception:
            log.debug("soak observer record sink failed", exc_info=True)
        if self.hub is not None:
            try:
                self.hub.publish_threadsafe(f"serial.{held.serial}", {"line": rec["line"]})
            except Exception:
                pass

    def _emit_status(self, held: _Held, line: str) -> None:
        self._emit(
            held,
            {
                "ts": time.time(),
                "serial": held.serial,
                "port": held.port,
                "kind": "status",
                "line": line,
                "level": None,
                "tag": None,
                "heap_free": None,
                "uptime_s": None,
            },
        )

    def _on_log_line(self, line: str, interface: Any = None) -> None:
        held = self._resolve(interface)
        if held is None:
            return
        try:
            parsed = parsers.parse_log_line(str(line))
            self._emit(
                held,
                {
                    "ts": time.time(),
                    "serial": held.serial,
                    "port": held.port,
                    "kind": "log",
                    "line": parsed["line"],
                    "level": parsed.get("level"),
                    "tag": parsed.get("tag"),
                    "heap_free": parsed.get("heap_free"),
                    "uptime_s": parsed.get("uptime_s"),
                },
            )
        except Exception:
            log.debug("soak observer log handler failed", exc_info=True)

    def _on_receive(self, packet: dict[str, Any], interface: Any = None) -> None:
        held = self._resolve(interface)
        if held is None:
            return
        try:
            decoded = packet.get("decoded") or {}
            portnum = decoded.get("portnum") or "ENCRYPTED"
            sender = _node_id(packet.get("from"))
            text = decoded.get("text")
            if portnum == "TEXT_MESSAGE_APP" and text is not None:
                line = f"RX text from {sender} ch{packet.get('channel', 0)}: {text}"
            else:
                line = f"RX {portnum} from {sender}"
            rec = {
                "ts": time.time(),
                "serial": held.serial,
                "port": held.port,
                "kind": "packet",
                "line": line,
                "level": None,
                "tag": None,
                "heap_free": None,
                "uptime_s": None,
                "from_node": packet.get("from"),
                "portnum": portnum,
            }
            if text is not None:
                rec["text"] = text
            self._emit(held, rec)
        except Exception:
            log.debug("soak observer receive handler failed", exc_info=True)

    def _on_telemetry_packet(self, packet: dict[str, Any], interface: Any = None) -> None:
        held = self._resolve(interface)
        if held is None:
            return
        try:
            telem = (packet.get("decoded") or {}).get("telemetry") or {}
            metrics = telem.get("deviceMetrics") or {}
            if not metrics:
                return
            from_num = packet.get("from")
            # Attribute the point to the node the metrics are ABOUT (the
            # sender), falling back to the observing device's serial.
            about = self.node_map.get(from_num) if from_num is not None else None
            base = {
                "ts": time.time(),
                "serial": about or held.serial,
                "port": held.port,
                "from_node": from_num,
            }
            battery = metrics.get("batteryLevel")
            if battery is not None:
                self.on_telemetry({**base, "kind": "battery", "value": battery})
            chutil = metrics.get("channelUtilization")
            if chutil is not None:
                self.on_telemetry({**base, "kind": "channel_utilization", "value": chutil})
        except Exception:
            log.debug("soak observer telemetry handler failed", exc_info=True)

    def _on_connection_lost(self, interface: Any = None) -> None:
        held = self._resolve(interface)
        if held is None or held.lost:
            return
        held.lost = True
        self._emit_status(held, "— API connection lost —")
