"""Datadog forwarder.

Pure mappers (``log_to_dd`` / ``telemetry_to_metrics``) turn recorder rows into
dashboard-compatible Datadog payloads; ``_read_live`` is a cursor-based tail of
the recorder's JSONL streams; ``DDConfig`` persists settings (with the API key
masked on the way out). The shipping loop lives in ``DDForwarder``.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..db import repo_devices as rd
from ..db import repo_settings as rs
from .scrub import Scrubber

log = logging.getLogger("meshtastic_mcp.web.datadog")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_DDSOURCE = "meshtastic-firmware"

# Levels kept local by default (volume) — matches FleetLog's bench/fleet schema.
_SKIP_LEVELS = {"DEBUG", "TRACE", "HEAP"}

# Level → Datadog status, aligned with the FleetLog forwarder so bench + fleet
# rows share one dashboard (warn/critical, not warning/error).
_STATUS = {
    "DEBUG": "debug",
    "TRACE": "debug",
    "HEAP": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "CRIT": "critical",
    "CRITICAL": "critical",
}


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s or "")


def _status_for(level: str | None) -> str:
    # Un-leveled output (panic/backtrace) has no prefix; FleetLog ships it as
    # "info" and lets the message content carry the crash.
    return _STATUS.get((level or "").upper(), "info")


def _sanitize_tag_value(value) -> str:
    """Datadog tag value: lowercase, with space/comma/colon collapsed to '_'."""
    s = str(value).strip().lower()
    for ch in (" ", ",", ":"):
        s = s.replace(ch, "_")
    return s or "unknown"


def _tag(key: str, value) -> str | None:
    if value is None or value == "":
        return None
    return f"{key}:{_sanitize_tag_value(value)}"


def _redact_secret(text: str) -> str:
    """Strip a ``dd-api-key=<token>`` value from a string (client-token intake
    URLs carry the token in the query string, and request errors echo it)."""
    return re.sub(r"(dd-api-key=)[^&\s]+", r"\1***", text)


# --- log mapping ------------------------------------------------------------
def log_to_dd(
    rec: dict,
    *,
    host: str,
    base_tags: list[str],
    port_tags: dict[str, list[str]],
    scrubber: Scrubber,
    ship_debug: bool,
) -> dict | None:
    """Map a recorder log row to a Datadog log intake payload.

    Returns None for a DEBUG/TRACE/HEAP line when ``ship_debug`` is False.
    Un-leveled lines (panics/backtraces) always ship.
    """
    level = rec.get("level")
    if level and level.upper() in _SKIP_LEVELS and not ship_debug:
        return None

    message = scrubber.scrub(_strip_ansi(rec.get("line", "")))

    tags = list(base_tags)
    port = rec.get("port")
    if port:
        tags.append(f"port:{_sanitize_tag_value(port)}")
        tags.extend(port_tags.get(port, []))
    if level:
        tags.append(f"level:{_sanitize_tag_value(level)}")
    tag = rec.get("tag")
    if tag:
        tags.append(f"thread:{_sanitize_tag_value(tag)}")

    payload = {
        "ddsource": _DDSOURCE,
        "service": _DDSOURCE,
        "hostname": host,
        "message": message,
        "ddtags": ",".join(tags),
        "status": _status_for(level),
    }
    if level is not None:
        payload["level"] = level
    if rec.get("ts") is not None:
        payload["timestamp"] = round(rec["ts"] * 1000)
    if isinstance(rec.get("heap_free"), int):
        payload["heap_free"] = rec["heap_free"]
    if isinstance(rec.get("uptime_s"), int):
        payload["uptime_s"] = rec["uptime_s"]
    return payload


# --- metric mapping ---------------------------------------------------------
def telemetry_to_metrics(
    rec: dict,
    *,
    host: str,
    base_tags: list[str],
    port_tags: dict[str, list[str]],
) -> list[dict]:
    """Map a telemetry row to Datadog GAUGE series (one per numeric field).
    Boolean and string fields are dropped."""
    variant = rec.get("variant", "")
    fields_ = rec.get("fields", {}) or {}
    ts = int(rec.get("ts", 0) or 0)

    tags = [*list(base_tags), f"variant:{variant}"]
    port = rec.get("port")
    if port:
        tags.extend(port_tags.get(port, []))

    out: list[dict] = []
    for key, value in fields_.items():
        # bool is a subclass of int — exclude it explicitly.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append(
            {
                "metric": f"mesh.{variant}.{key}",
                "type": 3,  # GAUGE
                "points": [{"timestamp": ts, "value": float(value)}],
                "resources": [{"type": "host", "name": host}],
                "tags": list(tags),
            }
        )
    return out


# --- cursor-based JSONL tail ------------------------------------------------
def _read_live(path: Path, cursor: dict, max_lines: int) -> tuple[list[dict], dict]:
    """Read newly-appended complete JSON lines from ``path``.

    ``cursor`` is ``{"ino": int, "pos": int}``. A partial trailing line (no
    newline yet) is left for the next cycle. A cursor whose inode no longer
    matches, or whose position is past EOF (rotation/truncation), resets to the
    start. Returns ``(rows, next_cursor)``.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return [], dict(cursor)
    ino = st.st_ino

    pos = cursor.get("pos", 0) if cursor.get("ino") == ino else 0
    if pos > st.st_size:  # truncated or rotated under us
        pos = 0

    with open(path, "rb") as fh:
        fh.seek(pos)
        data = fh.read()

    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        # No complete line available — leave the cursor where it was.
        return [], {"ino": ino, "pos": pos}

    complete = data[: last_nl + 1]
    consumed = pos + len(complete)

    rows: list[dict] = []
    offset = 0
    for raw in complete.splitlines(keepends=True):
        offset += len(raw)
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
        if len(rows) >= max_lines:
            # Stop the cursor after the last returned row so the rest of the
            # chunk is picked up next cycle instead of being skipped.
            return rows, {"ino": ino, "pos": pos + offset}
    return rows, {"ino": ino, "pos": consumed}


# --- intake host derivation -------------------------------------------------
def _browser_intake_origin(site: str) -> str:
    """The RUM/browser-logs intake origin for a Datadog site.

    ``us5.datadoghq.com`` → ``https://browser-intake-us5-datadoghq.com``
    ``datadoghq.eu``      → ``https://browser-intake-datadoghq.eu``
    """
    head, _, tld = site.rpartition(".")
    head = head.replace(".", "-")
    return f"https://browser-intake-{head}.{tld}" if head else f"https://browser-intake-{tld}"


def _logs_intake_url(site: str) -> str:
    return f"https://http-intake.logs.{site}/api/v2/logs"


def _metrics_intake_url(site: str) -> str:
    return f"https://api.{site}/api/v2/series"


# --- config -----------------------------------------------------------------
# FleetSuite ships to US5 only (matches the FleetLog fleet + the shared
# dashboard); the site is not user-configurable.
DD_SITE = "us5.datadoghq.com"


@dataclass
class DDConfig:
    enabled: bool = False
    api_key: str = ""
    site: str = DD_SITE
    scrub: str = "redact"  # strongest by default (matches FleetLog)
    collector: str = "fleetsuite"
    host: str = ""  # tester / bench id → Datadog hostname + tester: tag
    ship_debug: bool = False

    def masked(self) -> dict:
        """Config for the UI — the API key is never exposed, only a hint and a
        client-token flag (Datadog client tokens start with ``pub``)."""
        return {
            "enabled": self.enabled,
            "site": self.site,
            "scrub": self.scrub,
            "collector": self.collector,
            "host": self.host,
            "ship_debug": self.ship_debug,
            "has_key": bool(self.api_key),
            "key_hint": self.api_key[-4:] if self.api_key else "",
            "is_client_token": self.api_key.startswith("pub") if self.api_key else False,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> DDConfig:
        d = d or {}
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


async def load_config(db) -> DDConfig:
    return DDConfig.from_dict(await rs.get_json(db, "datadog"))


async def save_config(db, cfg: DDConfig) -> None:
    await rs.set_json(db, "datadog", asdict(cfg))


# --- forwarder (runtime) ----------------------------------------------------
def _recorder_dir() -> Path:
    return Path(os.environ.get("MESHTASTIC_MCP_RECORDER_DIR", ".mtlog"))


_FLUSH_SECONDS = 2.0
_BATCH_MAX = 500


class DDForwarder:
    """Continuous device-log forwarder — FleetSuite's FleetLog. While enabled it
    holds a live capture on every connected node and ships their firmware logs to
    Datadog Logs in batches, independent of test runs.

    Log lines arrive via :meth:`submit` (the serial monitors call it for every
    captured line). A background flush loop scrubs + maps them with
    :func:`log_to_dd` and POSTs to the logs intake. :meth:`sync_capture` (driven
    by discovery) keeps a logging hold on every online USB device so capture is
    fleet-wide, not just whatever a UI tab happens to be watching.
    """

    def __init__(self, db, hub, serialmon=None) -> None:
        self.db = db
        self.hub = hub
        self.serialmon = serialmon
        self.cfg = DDConfig()
        self.stats = {
            "running": False,
            "sent_logs": 0,
            "sent_metrics": 0,
            "cycles": 0,
            "last_error": None,
            "last_cycle_ts": None,
        }
        self._queue: collections.deque = collections.deque(maxlen=20000)
        self._scrubber = Scrubber(self.cfg.scrub)
        self._task: asyncio.Task | None = None
        self._captured: set[str] = set()  # serials we hold a logging monitor on
        self._capture_lock = asyncio.Lock()  # serialise capture acquire/release
        self._dropped = 0  # records dropped on queue overflow
        self._fail_count = 0
        self._backoff_until = 0.0  # loop time to resume shipping after failures
        # Dedicated, bounded pool for the blocking HTTP ship. requests.post has a
        # 30s timeout, so a slow/unreachable Datadog intake can park its worker
        # that long; isolating it here keeps that stall off the shared default
        # pool the control path depends on. One worker suffices — the flush loop
        # ships one batch at a time and awaits it before starting the next.
        self._http_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="datadog-http"
        )

    def status(self) -> dict:
        stats = dict(self.stats)
        stats["dropped"] = self._dropped
        return {"config": self.cfg.masked(), "stats": stats}

    def active(self) -> bool:
        # Always ship once a token is present — there is no separate enable flag.
        return bool(self.cfg.api_key)

    # --- ingest -----------------------------------------------------------
    def submit(self, rec: dict) -> None:
        """Enqueue a captured log record (called from serial-monitor threads —
        ``deque.append`` is atomic, so no lock needed). Dropped when inactive."""
        if not self.active():
            return
        if len(self._queue) >= self._queue.maxlen:
            self._dropped += 1  # deque will evict the oldest; record the loss
        self._queue.append(rec)

    # --- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._release_all_capture()
        # Don't wait — an in-flight POST may be parked on its 30s timeout; the
        # worker drains on its own and never touches the default pool.
        self._http_executor.shutdown(wait=False)

    async def reload(self) -> None:
        self.cfg = await load_config(self.db)
        self.cfg.site = DD_SITE  # FleetSuite always ships to US5
        self._scrubber = Scrubber(self.cfg.scrub)
        await self._sync_capture_state()

    async def _flush_loop(self) -> None:
        while True:
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["last_error"] = str(exc)
                log.debug("datadog flush error: %s", exc)
            await asyncio.sleep(_FLUSH_SECONDS)

    async def _flush_once(self) -> None:
        cfg = self.cfg  # atomic snapshot; reload() swaps the whole ref
        running = bool(cfg.api_key)
        was_running = self.stats["running"]
        self.stats["running"] = running
        if not running:
            self._queue.clear()
            if was_running:
                await self.hub.publish("datadog.update", self.status())
            return

        loop = asyncio.get_running_loop()
        if self._backoff_until and loop.time() < self._backoff_until:
            return  # backing off after repeated failures; keep the queue

        batch: list[dict] = []
        while self._queue and len(batch) < _BATCH_MAX:
            batch.append(self._queue.popleft())

        if batch:
            port_tags = await self._port_tags()
            # Datadog hostname is the tester/bench id (a machine hostname is
            # PII-ish and meaningless fleet-wide), mirrored in a tester: tag.
            host = cfg.host or cfg.collector or "fleetsuite"
            base_tags = [
                _tag("collector", cfg.collector) or "collector:fleetsuite",
                f"tester:{_sanitize_tag_value(host)}",
            ]
            payloads = []
            for rec in batch:
                m = log_to_dd(
                    rec,
                    host=host,
                    base_tags=base_tags,
                    port_tags=port_tags,
                    scrubber=self._scrubber,
                    ship_debug=cfg.ship_debug,
                )
                if m is not None:
                    payloads.append(m)
            if payloads:
                ok, err = await loop.run_in_executor(
                    self._http_executor, self._post_logs, cfg, payloads
                )
                if ok:
                    self.stats["sent_logs"] += len(payloads)
                    self.stats["last_error"] = None
                    self._fail_count = 0
                    self._backoff_until = 0.0
                else:
                    self.stats["last_error"] = err
                    self._fail_count += 1
                    # exponential backoff, capped at 60s, to avoid hammering.
                    self._backoff_until = loop.time() + min(60.0, 2.0**self._fail_count)

        self.stats["cycles"] += 1
        self.stats["last_cycle_ts"] = time.time()
        await self.hub.publish("datadog.update", self.status())

    def _post_logs(self, cfg: DDConfig, payloads: list[dict]) -> tuple[bool, str | None]:
        """Gzipped POST to the Datadog logs intake. An API key uses the header
        intake; a client token (``pub…``) uses the browser intake with the token
        in the query string (FleetLog's scheme). A poisoned batch (400/413) is
        dropped rather than wedging the queue behind it forever."""
        try:
            import gzip

            import requests

            headers = {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
            if cfg.api_key.startswith("pub"):
                origin = _browser_intake_origin(cfg.site)
                url = (
                    f"{origin}/api/v2/logs?dd-api-key={cfg.api_key}"
                    f"&dd-evp-origin=browser&ddsource={_DDSOURCE}"
                )
            else:
                url = _logs_intake_url(cfg.site)
                headers["DD-API-KEY"] = cfg.api_key

            body = gzip.compress(json.dumps(payloads, default=str).encode("utf-8"))
            resp = requests.post(url, data=body, headers=headers, timeout=30)
            if resp.ok:
                return True, None
            if resp.status_code in (400, 413):
                # Datadog rejected the batch shape — drop it (report "shipped")
                # so one bad batch can't permanently stall the queue.
                log.warning("datadog rejected a batch (HTTP %s); dropping", resp.status_code)
                return True, f"dropped a batch: HTTP {resp.status_code}"
            return False, _redact_secret(f"HTTP {resp.status_code}: {resp.text[:160]}")
        except Exception as exc:
            return False, _redact_secret(str(exc))

    async def _port_tags(self) -> dict[str, list[str]]:
        """Per-port Datadog tags from the live registry. Uses FleetLog's per-node
        scheme (fw_version/hw_model/app_env/node_id) so bench + fleet rows share
        one dashboard, plus FleetSuite extras (role/hub/device)."""
        tags: dict[str, list[str]] = {}
        for d in await rd.list_all(self.db):
            port = d.get("current_port")
            if not port:
                continue
            node = d.get("node_num")
            node_id = f"!{node:08x}" if isinstance(node, int) else None
            hub = (
                f"{d['hub_location']}:{d.get('hub_port')}"
                if d.get("hub_location") is not None
                else None
            )
            t = [
                _tag("fw_version", d.get("firmware_version")),
                _tag("hw_model", d.get("hw_model")),
                _tag("app_env", d.get("env")),
                _tag("node_id", node_id),
                _tag("role", d.get("role")),
                _tag("hub", hub),
                _tag("device", d.get("serial_number")),
            ]
            tags[port] = [x for x in t if x]
        return tags

    # --- fleet-wide capture ----------------------------------------------
    async def sync_capture(self, online_usb_serials: set[str]) -> None:
        """Hold a logging monitor on every online USB device while forwarding is
        on (so capture is fleet-wide). Called each discovery scan. Suspended
        during a test run, which owns the ports."""
        async with self._capture_lock:
            await self._apply_capture(set(online_usb_serials))

    async def _sync_capture_state(self) -> None:
        """Reconcile capture against the current config (called by reload)."""
        async with self._capture_lock:
            if self.active():
                rows = await rd.list_all(self.db)
                want = {
                    d["serial_number"] for d in rows if d.get("online") and d.get("kind") == "usb"
                }
            else:
                want = set()
            await self._apply_capture(want)

    async def _release_all_capture(self) -> None:
        async with self._capture_lock:
            await self._apply_capture(set())

    async def _apply_capture(self, want: set[str]) -> None:
        """Drive ``self._captured`` toward ``want``. Caller holds ``_capture_lock``.
        Re-gates under the lock so a test run starting mid-reconcile wins."""
        from . import test_runner

        if not self.active() or self.serialmon is None or test_runner.is_running():
            want = set()
        for serial in want - self._captured:
            await self.serialmon.acquire(serial)
            self._captured.add(serial)
        for serial in self._captured - want:
            await self.serialmon.release(serial)
            self._captured.discard(serial)

    def test_key(self) -> dict:
        """Validate the configured API key against Datadog. Best-effort —
        returns ``{ok, error}`` and never raises."""
        if not self.cfg.api_key:
            return {"ok": False, "error": "no API key configured"}
        try:
            import requests

            resp = requests.get(
                f"https://api.{self.cfg.site}/api/v1/validate",
                headers={"DD-API-KEY": self.cfg.api_key},
                timeout=10,
            )
            if resp.ok and resp.json().get("valid"):
                return {"ok": True, "error": None}
            return {"ok": False, "error": f"validation failed ({resp.status_code})"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
