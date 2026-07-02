# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Live serial monitors, one per device, multiplexed onto the ``serial.<serial>``
WebSocket topic.

A monitor is reference-counted by subscriber: the first client to open a
device's Serial tab spawns a pyserial reader thread that republishes each line;
the last to leave tears it down. Direct pyserial is used (not ``pio device
monitor``, whose miniterm backend requires a controlling TTY and crashes when
run headless under the server). Because the reader holds the USB port, any
control action ``suspend``s it for the duration and ``resume``s after, so the
port is never double-opened.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import serial as pyserial

from meshtastic_mcp import connection
from meshtastic_mcp.recorder.parsers import parse_log_line

from ..db import repo_devices as rd

log = logging.getLogger("meshtastic_mcp.web.serial_monitor")

BAUD = 115200
# Cap on the partial-line buffer: a binary burst or newline-free stream must
# not grow it without bound. Text debug lines are well under this.
MAX_PARTIAL = 8192


class _Monitor:
    def __init__(self) -> None:
        self.refs = 0
        self.suspended = False
        self.stop: threading.Event | None = None
        self.thread: threading.Thread | None = None


class SerialMonitor:
    def __init__(self, db, hub) -> None:
        self.db = db
        self.hub = hub
        self.forwarder = None  # set by app wiring; receives captured log lines
        self._mons: dict[str, _Monitor] = {}
        # Per-serial lock serialising acquire/release/suspend/resume/open/close,
        # so overlapping callers (discovery sync, enrichment, control, UI, the
        # forwarder) can't double-open a port or corrupt the ref count.
        self._locks: dict[str, asyncio.Lock] = {}

    def _topic(self, serial: str) -> str:
        return f"serial.{serial}"

    def _lock(self, serial: str) -> asyncio.Lock:
        lk = self._locks.get(serial)
        if lk is None:
            lk = self._locks[serial] = asyncio.Lock()
        return lk

    async def acquire(self, serial: str) -> None:
        async with self._lock(serial):
            mon = self._mons.setdefault(serial, _Monitor())
            mon.refs += 1
            if mon.refs == 1 and not mon.suspended:
                await self._open(serial, mon)

    async def release(self, serial: str) -> None:
        async with self._lock(serial):
            mon = self._mons.get(serial)
            if mon is None:
                return
            mon.refs = max(0, mon.refs - 1)
            if mon.refs == 0:
                await self._close(mon)
                self._mons.pop(serial, None)

    async def suspend(self, serial: str) -> None:
        """Free the port for a control action (no-op if not monitored)."""
        async with self._lock(serial):
            mon = self._mons.get(serial)
            if mon is None:
                return
            mon.suspended = True
            await self._close(mon)

    async def resume(self, serial: str) -> None:
        async with self._lock(serial):
            mon = self._mons.get(serial)
            if mon is None:
                return
            mon.suspended = False
            # `_open` self-heals an abandoned (now-dead) reader, so resume only
            # needs to skip a monitor that is genuinely still running.
            if mon.refs > 0 and (mon.thread is None or not mon.thread.is_alive()):
                await self._open(serial, mon)

    async def suspend_all(self) -> None:
        """Free every monitored port (e.g. for a hub-wide identify sweep)."""
        for serial in list(self._mons):
            await self.suspend(serial)

    async def resume_all(self) -> None:
        for serial in list(self._mons):
            await self.resume(serial)

    async def shutdown(self) -> None:
        for mon in list(self._mons.values()):
            await self._close(mon)
        self._mons.clear()

    async def _open(self, serial: str, mon: _Monitor) -> None:
        # A test run owns the whole serial bus: the pytest subprocess opens the
        # ports exclusively (flash, transmit-history reset, …). The server must
        # never hold a port mid-run, no matter who asks (discovery/forwarder
        # capture, a UI serial tab, keep-alive). suspend_all() frees them at
        # launch; this guard stops anything from re-opening one until the run
        # ends, when resume_all() re-establishes the monitors.
        from . import test_runner

        if test_runner.is_running():
            return
        # Self-heal an abandoned close: if a previous _close() timed out, the
        # wedged reader kept mon.thread set so nothing could double-open the
        # port. Once that thread has died (device re-enumerated after a
        # power-cycle, or the blocked ioctl finally errored), the stale
        # handle is safe to clear — without this, one close timeout killed
        # the monitor for the rest of the process lifetime and the port
        # stayed EIO-wedged by our own abandoned fd.
        if mon.thread is not None:
            if mon.thread.is_alive():
                return  # still genuinely held — never spawn a second reader
            mon.thread = None
            mon.stop = None
        row = await rd.get(self.db, serial)
        if row is None or row.get("kind") == "native":
            return  # native nodes are TCP — nothing to monitor on the USB bus
        port = row.get("current_port")
        if not port or connection.is_tcp_port(port):
            return
        mon.stop = threading.Event()
        mon.thread = threading.Thread(
            target=self._read_loop, args=(serial, port, mon.stop), daemon=True
        )
        mon.thread.start()

    async def _close(self, mon: _Monitor) -> None:
        if mon.stop is not None:
            mon.stop.set()
        thread = mon.thread
        # Keep mon.thread set until the worker has fully exited, so a concurrent
        # resume()/_open() (under the same per-serial lock) never spawns a second
        # reader against a port the old thread is still releasing.
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
            if thread.is_alive():
                # Abandon it but keep mon.thread set: the fd is still held, so a
                # second reader must not open the port. _open()/is_wedged() treat
                # a dead abandoned thread as closed, so recovery (power-cycle →
                # re-enumeration kills the reader → resume) reopens the monitor.
                log.warning(
                    "serial monitor thread did not stop within timeout — "
                    "abandoning it (self-heals after the reader dies; "
                    "an unwedge/power-cycle forces that)"
                )
                return
        mon.thread = None
        mon.stop = None

    def is_wedged(self, serial: str) -> bool:
        """True when this device's reader was abandoned by a timed-out close
        and is STILL alive — i.e. our own process holds the port hostage and
        only a power-cycle (device re-enumeration) will free it."""
        mon = self._mons.get(serial)
        return (
            mon is not None and mon.thread is not None and mon.thread.is_alive() and mon.suspended
        )

    def _read_loop(self, serial: str, port: str, stop: threading.Event) -> None:
        """Runs in a worker thread; publishes lines via the hub's thread-safe
        path. Reads with a short timeout so ``stop`` is honoured promptly."""
        topic = self._topic(serial)
        try:
            ser = pyserial.Serial(port, BAUD, timeout=0.5)
        except Exception as exc:
            from ... import port_recovery

            self.hub.publish_threadsafe(
                topic,
                {"line": f"— cannot open {port}: {exc} [{port_recovery.classify(exc)}] —"},
            )
            return
        self.hub.publish_threadsafe(topic, {"line": f"— monitor opened on {port} —"})
        buf = b""
        try:
            while not stop.is_set():
                try:
                    data = ser.read(256)
                except Exception as exc:
                    self.hub.publish_threadsafe(topic, {"line": f"— read error: {exc} —"})
                    break
                if not data:
                    continue
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").rstrip("\r")
                    if not text:
                        continue
                    # The meshtastic CDC carries protobuf API frames interleaved
                    # with text debug logs. Drop lines that are mostly undecodable
                    # bytes (a protobuf frame) — decoded text logs render with ANSI.
                    bad = text.count("�")
                    if bad and bad > len(text) * 0.2:
                        continue
                    self.hub.publish_threadsafe(topic, {"line": text})
                    # FleetLog: forward every captured line to Datadog when on.
                    fwd = self.forwarder
                    if fwd is not None and fwd.active():
                        parsed = parse_log_line(text)
                        fwd.submit(
                            {
                                "ts": time.time(),
                                "port": port,
                                "line": text,
                                "level": parsed.get("level"),
                                "tag": parsed.get("tag"),
                                "heap_free": parsed.get("heap_free"),
                                "uptime_s": parsed.get("uptime_s"),
                            }
                        )
                if len(buf) > MAX_PARTIAL:
                    buf = b""  # drop runaway newline-free data; resync at next newline
        finally:
            try:
                ser.close()
            except Exception:
                pass
