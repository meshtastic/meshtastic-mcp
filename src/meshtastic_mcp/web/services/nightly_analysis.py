# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Nightly run analysis: test failures + noteworthy observations.

Deterministic heuristics run over three sources — the pipeline's own
observation trail, the MCP recorder window the pytest child wrote during the
suite, and the soak's JSONL capture — and work with no local model at all.
When a local model is reachable it adds a *behavioral* pass: chunked
map-reduce summaries of the night's logs and a vision/OCR check of each
screen snapshot. Model output is always labeled a draft, never a verdict.

Log lines are device-authored (remote mesh nodes can inject content into
them) — evidence is ANSI-stripped, GPS-scrubbed, and only ever rendered
inside fenced blocks for a human reader.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from meshtastic_mcp import local_model, log_query
from meshtastic_mcp.capabilities import has_local_model

from ..db import repo_devices as rd
from . import nightly_soak
from .nightly import NightlyConfig, commit_subjects, nightly_fw_dir
from .scrub import Scrubber
from .test_runner import _load_bench, _nodeid_param

log = logging.getLogger("meshtastic_mcp.web.nightly_analysis")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Crash/panic markers, case-insensitive. Copied from
# tests/monitor/test_boot_log_no_panic.py::_PANIC_MARKERS (tests/ is not an
# installed package) — keep the two lists in sync — plus reset-reason and
# brownout patterns that matter for an unattended soak.
PANIC_MARKERS = [
    "guru meditation",
    "corrupt heap",
    "abort()",
    "assertion failed",
    "***",
    "panic",
    "stack overflow",
    "load prohibited",
    "store prohibited",
    "illegalinstr",
    "watchdog got triggered",
    "brownout",
]
_PANIC_PATTERN = "|".join(re.escape(m) for m in PANIC_MARKERS) + r"|reset reason: (panic|wdt)"
_PANIC_RE = re.compile(_PANIC_PATTERN, re.IGNORECASE)
# log_query.logs_window re-compiles a grep string WITHOUT flags, so bake the
# case-insensitivity into the pattern itself with an inline (?i).
_PANIC_GREP = f"(?i){_PANIC_PATTERN}"

ERROR_LOG_WARN_THRESHOLD = 5
REBOOT_CHURN_THRESHOLD = 10
BATTERY_SLOPE_WARN = -0.2  # percent/min  (≈ >12%/hour)
BATTERY_MIN_SAMPLES = 10
HEAP_SLOPE_WARN = -100.0  # bytes/min
HEAP_MIN_SAMPLES = 20
EVIDENCE_MAX_LINES = 10

# Local-model budget: existing offload tools byte-cap prompts around 20-24 KB;
# stay under that per chunk and cap how many chunks a night may spend.
LLM_CHUNK_BYTES = 18_000
LLM_MAX_CHUNKS_PER_DEVICE = 8
LLM_TIMEOUT_S = 120.0

BEHAVIOR_SYSTEM = (
    "You are a Meshtastic device-log triage assistant reviewing an overnight "
    "soak of a private test mesh. Reply with terse bullets only — no preamble. "
    "Be specific (node ids, portnums, error strings, counts). Flag: reboots, "
    "retransmission storms, unexpected silence, odd timing, anything a bench "
    "operator should look at. Say 'nothing notable' if the window is clean."
)
VISION_QUESTION = (
    "Does this device screen show an error, crash, garbled rendering, or a blank/frozen display?"
)


@dataclass
class Observation:
    severity: str  # info | warn | error
    category: str
    summary: str
    device: str | None = None
    evidence: list[str] = field(default_factory=list)
    data: dict | None = None


@dataclass
class Failure:
    nodeid: str
    tier: str | None
    device: str | None
    duration_s: float | None
    longrepr: str | None


@dataclass
class AnalysisResult:
    failures: list[Failure] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    device_rows: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def _clean(line: str, scrubber: Scrubber) -> str:
    return scrubber.scrub(_ANSI.sub("", line or ""))


def _slope_per_min(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope in units/minute over (ts, value) points."""
    n = len(points)
    if n < 2:
        return None
    xs = [(ts - points[0][0]) / 60.0 for ts, _v in points]
    ys = [v for _ts, v in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for ln in fh:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def _device_label(row: dict) -> str:
    return row.get("friendly_name") or row.get("serial_number") or "?"


class Analyzer:
    """One night's analysis pass. Constructed fresh per report."""

    def __init__(
        self,
        db,
        *,
        cfg: NightlyConfig,
        nightly: dict,
        run: dict | None,
        results: list[dict],
        pipeline_obs: list[dict],
        data_dir: Path,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.nightly = nightly
        self.run = run
        self.results = results
        self.pipeline_obs = pipeline_obs
        self.data_dir = data_dir
        self.scrubber = Scrubber("redact")
        self.out = AnalysisResult()
        # Loaded once, shared by heuristics:
        self.devices: list[dict] = []
        self.soak_logs: list[dict] = []
        self.soak_telemetry: list[dict] = []
        self.soak_sends: list[dict] = []

    # -- entry ---------------------------------------------------------------

    async def analyze(self) -> AnalysisResult:
        self.devices = await rd.list_all(self.db)
        # A 2h soak across several boards can be tens of MB — read off-loop so a
        # big night doesn't stall the event loop while parsing JSONL.
        self.soak_logs = await asyncio.to_thread(
            _read_jsonl, self.data_dir / nightly_soak.LOGS_FILE
        )
        self.soak_telemetry = await asyncio.to_thread(
            _read_jsonl, self.data_dir / nightly_soak.TELEMETRY_FILE
        )
        self.soak_sends = await asyncio.to_thread(
            _read_jsonl, self.data_dir / nightly_soak.SENDS_FILE
        )

        self._counts_and_failures()
        self._step_errors()
        await self._version_change()
        self._device_missing()
        self._device_not_baked()
        await self._suite_window_scan()
        self._soak_scan()
        self._telemetry_slopes()
        self._traffic_loss()
        self._device_rows()
        await self._behavioral()
        return self.out

    # -- deterministic heuristics --------------------------------------------

    def _counts_and_failures(self) -> None:
        run = self.run or {}
        self.out.counts = {
            "passed": run.get("passed", 0),
            "failed": run.get("failed", 0),
            "skipped": run.get("skipped", 0),
            "exit_code": run.get("exit_code"),
        }
        for r in self.results:
            if r.get("outcome") == "failed":
                self.out.failures.append(
                    Failure(
                        nodeid=r.get("nodeid") or "?",
                        tier=r.get("tier"),
                        device=r.get("device_serial"),
                        duration_s=r.get("duration_s"),
                        longrepr=r.get("longrepr"),
                    )
                )

    def _step_errors(self) -> None:
        for o in self.pipeline_obs:
            if o.get("severity") != "error":
                continue
            category = (
                "channel_default" if str(o.get("kind", "")).startswith("channel.") else "step_error"
            )
            self.out.observations.append(
                Observation(
                    severity="error",
                    category=category,
                    summary=f"[{o.get('step')}] {o.get('message')}",
                    device=(o.get("data") or {}).get("serial"),
                    data=o.get("data"),
                )
            )

    async def _version_change(self) -> None:
        old, new = self.nightly.get("fw_sha_before"), self.nightly.get("fw_sha_after")
        if old and new and old != new:
            subjects = await asyncio.to_thread(commit_subjects, nightly_fw_dir(), old, new)
            self.out.observations.append(
                Observation(
                    severity="info",
                    category="version_change",
                    summary=f"firmware develop moved {old[:7]} → {new[:7]}",
                    evidence=subjects,
                    data={
                        "compare_url": "https://github.com/meshtastic/firmware/"
                        f"compare/{old}...{new}"
                    },
                )
            )
        mcp_old, mcp_new = self.nightly.get("mcp_sha_before"), self.nightly.get("mcp_sha_after")
        if mcp_old and mcp_new and mcp_old != mcp_new:
            self.out.observations.append(
                Observation(
                    severity="info",
                    category="version_change",
                    summary=f"meshtastic-mcp moved {mcp_old[:7]} → {mcp_new[:7]}",
                )
            )

    def _fleet(self) -> list[dict]:
        return [d for d in self.devices if d.get("kind") == "usb" and d.get("env")]

    def _device_missing(self) -> None:
        started = self.nightly.get("started_at") or 0
        for d in self._fleet():
            if not d.get("online") or (d.get("last_seen") or 0) < started:
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="device_missing",
                        summary=f"{_device_label(d)} ({d.get('env')}) never enumerated "
                        "during the night",
                        device=d.get("serial_number"),
                    )
                )

    def _baked_roles(self) -> dict[str, str]:
        """serial -> bake outcome, attributed via the bench role's hub slot
        (the same resolution _record_bake_flashed uses). Degrades to {}."""
        bench = _load_bench()
        if bench is None:
            return {}
        by_slot = {
            (d.get("hub_location"), d.get("hub_port")): d.get("serial_number") for d in self.devices
        }
        out: dict[str, str] = {}
        for r in self.results:
            if r.get("tier") != "bake":
                continue
            role = _nodeid_param(r.get("nodeid") or "")
            if not role:
                continue
            try:
                hub_port = bench.location_hub_port(bench.role_location(role))
            except Exception:
                hub_port = None
            if not hub_port:
                continue
            serial = by_slot.get((hub_port[0], hub_port[1]))
            if serial:
                out[serial] = r.get("outcome") or "?"
        return out

    def _device_not_baked(self) -> None:
        fw_after = self.nightly.get("fw_sha_after")
        baked = self._baked_roles()
        for d in self._fleet():
            if not d.get("online"):
                continue  # already reported as device_missing
            serial = d.get("serial_number") or ""
            outcome = baked.get(serial)
            if self.run is not None and outcome is None and baked:
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="device_not_baked",
                        summary=f"{_device_label(d)} has no attributed bake result this run",
                        device=serial,
                    )
                )
            elif fw_after and outcome == "passed" and d.get("flashed_fw_sha") != fw_after:
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="device_not_baked",
                        summary=f"{_device_label(d)} baked green but registry sha "
                        f"{str(d.get('flashed_fw_sha'))[:7]} ≠ tonight's {fw_after[:7]}",
                        device=serial,
                    )
                )

    async def _suite_window_scan(self) -> None:
        """Panic/error/reboot heuristics over the recorder window the pytest
        child wrote during the suite."""
        started = self.nightly.get("started_at")
        end = self.nightly.get("soak_started_at") or self.nightly.get("finished_at")
        if not started or not end or self.run is None:
            return
        for d in self._fleet():
            port = d.get("current_port")
            serial = d.get("serial_number")
            if not port:
                continue
            try:
                panics = await asyncio.to_thread(
                    log_query.logs_window,
                    started,
                    end,
                    grep=_PANIC_GREP,
                    port=port,
                    max_lines=EVIDENCE_MAX_LINES,
                )
                errors = await asyncio.to_thread(
                    log_query.logs_window,
                    started,
                    end,
                    level="ERROR|CRIT",
                    port=port,
                    max_lines=EVIDENCE_MAX_LINES,
                )
            except Exception as exc:
                log.debug("suite window scan failed for %s: %s", serial, exc)
                continue
            self._panic_and_error_obs(serial, "suite", panics, errors)
        try:
            events = await asyncio.to_thread(
                log_query.events_window, started, end, kind="connection_lost", max=1000
            )
            drops: dict[str, int] = {}
            for ev in events.get("events", []):
                p = ev.get("port") or "?"
                drops[p] = drops.get(p, 0) + 1
            for p, n in drops.items():
                if n > REBOOT_CHURN_THRESHOLD:
                    self.out.observations.append(
                        Observation(
                            severity="warn",
                            category="reboot_churn",
                            summary=f"{p}: {n} connection losses during the suite",
                            data={"port": p, "count": n},
                        )
                    )
        except Exception as exc:
            log.debug("suite events scan failed: %s", exc)

    def _panic_and_error_obs(self, serial, window: str, panics: dict, errors: dict) -> None:
        p_lines = [_clean(r.get("line", ""), self.scrubber) for r in panics.get("lines", [])]
        if p_lines:
            self.out.observations.append(
                Observation(
                    severity="warn",
                    category="panic",
                    summary=f"{serial}: {panics.get('total_matched', len(p_lines))} "
                    f"panic marker(s) in the {window} window",
                    device=serial,
                    evidence=p_lines[:EVIDENCE_MAX_LINES],
                )
            )
        total_err = errors.get("total_matched", 0)
        if total_err > 0:
            e_lines = [_clean(r.get("line", ""), self.scrubber) for r in errors.get("lines", [])]
            # Dedup on the message after the level prefix so a repeating error
            # shows once with its count.
            seen: dict[str, int] = {}
            for ln in e_lines:
                key = ln.split("]", 1)[-1].strip() or ln
                seen[key] = seen.get(key, 0) + 1
            self.out.observations.append(
                Observation(
                    severity="warn" if total_err > ERROR_LOG_WARN_THRESHOLD else "info",
                    category="error_logs",
                    summary=f"{serial}: {total_err} ERROR/CRIT line(s) in the {window} window",
                    device=serial,
                    evidence=list(seen)[:EVIDENCE_MAX_LINES],
                    data={"count": total_err},
                )
            )

    def _soak_lines_by_serial(self) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {}
        for rec in self.soak_logs:
            by.setdefault(rec.get("serial") or rec.get("port") or "?", []).append(rec)
        return by

    def _soak_scan(self) -> None:
        if not self.nightly.get("soak_started_at"):
            return
        by_serial = self._soak_lines_by_serial()
        # Include any device that produced soak lines even if it is offline NOW
        # (a board that panicked mid-soak and dropped off the bus still has its
        # crash lines captured — those are exactly what we must analyze).
        watched = {
            d.get("serial_number") or "?": d
            for d in self._fleet()
            if d.get("online") or (d.get("serial_number") or "?") in by_serial
        }
        for serial, d in watched.items():
            recs = by_serial.get(serial, [])
            if not recs:
                self.out.observations.append(
                    Observation(
                        severity="info",
                        category="log_silence",
                        summary=f"{_device_label(d)} produced no soak log lines — "
                        "serial capture may have failed",
                        device=serial,
                    )
                )
                continue
            panic_lines = [
                _clean(r.get("line", ""), self.scrubber)
                for r in recs
                if _PANIC_RE.search(r.get("line") or "")
            ]
            error_recs = [r for r in recs if (r.get("level") or "").strip() in ("ERROR", "CRIT")]
            self._panic_and_error_obs(
                serial,
                "soak",
                {"lines": [{"line": ln} for ln in panic_lines], "total_matched": len(panic_lines)},
                {
                    "lines": [{"line": r.get("line", "")} for r in error_recs[:EVIDENCE_MAX_LINES]],
                    "total_matched": len(error_recs),
                },
            )
            # Reboot detection: firmware uptime going backwards means a reset.
            reboots = 0
            last_uptime: float | None = None
            for r in recs:
                up = r.get("uptime_s")
                if up is None:
                    continue
                if last_uptime is not None and up < last_uptime - 5:
                    reboots += 1
                last_uptime = up
            if reboots > 0:
                self.out.observations.append(
                    Observation(
                        severity="warn" if reboots > 2 else "info",
                        category="reboot_churn",
                        summary=f"{_device_label(d)} rebooted {reboots}× during the soak",
                        device=serial,
                        data={"count": reboots},
                    )
                )

    def _telemetry_slopes(self) -> None:
        series: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for t in self.soak_telemetry:
            key = (t.get("serial") or "?", t.get("kind") or "?")
            series.setdefault(key, []).append((float(t.get("ts", 0)), float(t.get("value", 0))))
        for (serial, kind), points in series.items():
            slope = _slope_per_min(sorted(points))
            if slope is None:
                continue
            if (
                kind == "battery"
                and len(points) >= BATTERY_MIN_SAMPLES
                and slope < BATTERY_SLOPE_WARN
            ):
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="battery_drain",
                        summary=f"{serial}: battery draining at {slope:.2f} %/min over the soak",
                        device=serial,
                        data={"slope_per_min": round(slope, 3), "samples": len(points)},
                    )
                )
            if kind == "heap" and len(points) >= HEAP_MIN_SAMPLES and slope < HEAP_SLOPE_WARN:
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="heap_leak",
                        summary=f"{serial}: free heap falling at {slope:.0f} B/min over the soak",
                        device=serial,
                        data={"slope_per_min": round(slope, 1), "samples": len(points)},
                    )
                )

    def _traffic_loss(self) -> None:
        if not self.soak_sends:
            return
        # With a single fleet node there is no possible observer, so "never seen
        # by any other node" is meaningless — reporting 100% loss would be a
        # false alarm every night on a one-board bench.
        if len({d.get("serial_number") for d in self._fleet()}) < 2:
            return
        by_serial = self._soak_lines_by_serial()
        lost: list[str] = []
        for send in self.soak_sends:
            if not send.get("ok"):
                lost.append(f"{send.get('text')} (send failed: {send.get('error')})")
                continue
            text, sender = send.get("text") or "", send.get("serial")
            seen = any(
                text in (r.get("line") or "")
                for serial, recs in by_serial.items()
                if serial != sender
                for r in recs
            )
            if not seen:
                lost.append(f"{text} (sent by {sender}, never seen by any other node)")
        if lost:
            self.out.observations.append(
                Observation(
                    severity="warn",
                    category="traffic_loss",
                    summary=f"{len(lost)}/{len(self.soak_sends)} soak test messages "
                    "not observed on the mesh",
                    evidence=lost[:EVIDENCE_MAX_LINES],
                    data={"lost": len(lost), "sent": len(self.soak_sends)},
                )
            )

    def _device_rows(self) -> None:
        baked = self._baked_roles()
        by_serial = self._soak_lines_by_serial()
        obs_by_device: dict[str, list[Observation]] = {}
        for o in self.out.observations:
            if o.device:
                obs_by_device.setdefault(o.device, []).append(o)
        for d in self._fleet():
            serial = d.get("serial_number") or "?"
            device_obs = obs_by_device.get(serial, [])
            self.out.device_rows.append(
                {
                    "device": _device_label(d),
                    "serial": serial,
                    "env": d.get("env"),
                    "online": bool(d.get("online")),
                    "bake": baked.get(serial, "—"),
                    "soak_lines": len(by_serial.get(serial, [])),
                    "panics": sum(1 for o in device_obs if o.category == "panic"),
                    "errors": sum(1 for o in device_obs if o.category == "error_logs"),
                }
            )

    # -- local-model behavioral pass ------------------------------------------

    async def _behavioral(self) -> None:
        ready = await asyncio.to_thread(has_local_model)
        if not ready and self.cfg.llm_autostart:
            try:
                from meshtastic_mcp import llama_server

                await asyncio.to_thread(llama_server.serve)
                ready = await asyncio.to_thread(has_local_model)
            except Exception as exc:
                log.info("llama-server autostart failed: %s", exc)
        if not ready:
            self.out.observations.append(
                Observation(
                    severity="warn",
                    category="llm_unavailable",
                    summary="no local model reachable — behavioral analysis skipped",
                )
            )
            return
        try:
            await self._behavioral_logs()
            await self._behavioral_snapshots()
        except Exception as exc:  # a model hiccup must never sink the report
            self.out.observations.append(
                Observation(
                    severity="warn",
                    category="llm_unavailable",
                    summary=f"behavioral analysis aborted: {exc}",
                )
            )

    def _chunks_for(self, recs: list[dict]) -> list[str]:
        chunks: list[str] = []
        buf: list[str] = []
        size = 0
        for r in recs:
            line = _clean(r.get("line", ""), self.scrubber)
            if not line:
                continue
            size += len(line) + 1
            buf.append(line)
            if size >= LLM_CHUNK_BYTES:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            if len(chunks) >= LLM_MAX_CHUNKS_PER_DEVICE:
                break
        if buf and len(chunks) < LLM_MAX_CHUNKS_PER_DEVICE:
            chunks.append("\n".join(buf))
        return chunks

    async def _behavioral_logs(self) -> None:
        by_serial = self._soak_lines_by_serial()
        device_summaries: dict[str, str] = {}
        for serial, recs in by_serial.items():
            chunks = self._chunks_for(recs)
            if not chunks:
                continue
            parts: list[str] = []
            for i, chunk in enumerate(chunks):
                prompt = (
                    f"Soak log window for device {serial} (part {i + 1}/{len(chunks)}):\n{chunk}"
                )
                try:
                    part = await asyncio.to_thread(
                        local_model.complete,
                        prompt,
                        system=BEHAVIOR_SYSTEM,
                        lane="fast",
                        num_predict=300,
                        timeout=LLM_TIMEOUT_S,
                    )
                except local_model.LocalModelError as exc:
                    log.info("behavioral chunk for %s failed: %s", serial, exc)
                    continue
                if part.strip():
                    parts.append(part.strip())
            if parts:
                device_summaries[serial] = "\n".join(parts)
        if not device_summaries:
            return
        det = "\n".join(
            f"- [{o.severity}] {o.category}: {o.summary}" for o in self.out.observations
        )
        fleet_prompt = (
            "Per-device soak summaries:\n\n"
            + "\n\n".join(f"## {s}\n{t}" for s, t in device_summaries.items())
            + f"\n\nDeterministic findings already known:\n{det or '- none'}\n\n"
            "Give cross-device behavioral observations a bench operator should "
            "read tomorrow morning. Bullets only."
        )
        try:
            fleet = await asyncio.to_thread(
                local_model.complete,
                fleet_prompt,
                system=BEHAVIOR_SYSTEM,
                lane="default",
                num_predict=400,
                timeout=LLM_TIMEOUT_S,
            )
        except local_model.LocalModelError as exc:
            log.info("behavioral fleet reduce failed: %s", exc)
            return
        self.out.observations.append(
            Observation(
                severity="info",
                category="behavior",
                summary="local-model behavioral summary (draft — verify before acting)",
                evidence=[ln for ln in fleet.splitlines() if ln.strip()][:30],
                data={"devices": sorted(device_summaries)},
            )
        )

    async def _behavioral_snapshots(self) -> None:
        snaps = sorted(self.data_dir.glob("snap-*.jpg"))
        if not snaps:
            return
        from meshtastic_mcp import ocr  # import-safe without the [ui] extra

        for snap in snaps:
            serial = snap.name.split("-")[1] if snap.name.count("-") >= 2 else None
            try:
                ocr_text = await asyncio.to_thread(ocr.ocr_text, snap.read_bytes())
            except Exception:
                ocr_text = ""
            try:
                verdict = await asyncio.to_thread(
                    local_model.vision_assert, str(snap), VISION_QUESTION
                )
            except local_model.LocalModelError as exc:
                log.info("vision check for %s failed: %s", snap.name, exc)
                continue
            if verdict.get("match"):
                evidence = [f"vision: {verdict.get('evidence') or verdict.get('answer')}"]
                if ocr_text.strip():
                    evidence.append(f"ocr: {ocr_text.strip()[:200]}")
                self.out.observations.append(
                    Observation(
                        severity="warn",
                        category="behavior",
                        summary=f"screen check flagged {snap.name} "
                        "(local-model draft — verify before acting)",
                        device=serial,
                        evidence=evidence,
                        data={"snapshot": snap.name},
                    )
                )


async def analyze(
    db,
    *,
    cfg: NightlyConfig,
    nightly: dict,
    run: dict | None,
    results: list[dict],
    pipeline_obs: list[dict],
    data_dir: Path,
) -> AnalysisResult:
    return await Analyzer(
        db,
        cfg=cfg,
        nightly=nightly,
        run=run,
        results=results,
        pipeline_obs=pipeline_obs,
        data_dir=data_dir,
    ).analyze()
