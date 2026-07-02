"""FastAPI application factory for FleetSuite.

``create_app()`` wires the registry, the broadcast hub, and the services
together in a lifespan, mounts the REST API + the single ``/ws`` socket, and
serves the built Vue SPA from ``web/static``. Blocking library calls (serial
I/O, pio, git) are dispatched to a thread so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from meshtastic_mcp import (
    admin,
    boards,
    fixtures,
    log_query,
    port_recovery,
)
from meshtastic_mcp import (
    flash as flash_lib,
)
from meshtastic_mcp import (
    info as mt_info,
)
from meshtastic_mcp.config import ConfigError

from .db import repo_builds as rb
from .db import repo_cameras as rc
from .db import repo_devices as rd
from .db import repo_flash as rf
from .db import repo_runs as rr
from .db.database import Database, default_db_path
from .services import (
    builder,
    camera_stream,
    control,
    datadog,
    discovery,
    firmware,
    identity,
    keepalive,
    native,
    portlock,
    power,
    recovery,
    serial_monitor,
    test_runner,
)
from .services.activity import Activity
from .services.control import ControlBusy
from .services.power import AmbiguousPort, NoPort
from .ws.hub import Connection, Hub

log = logging.getLogger("meshtastic_mcp.web")

STATIC_DIR = Path(__file__).parent / "static"


def _busy_guard(exc: ControlBusy) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def create_app() -> FastAPI:
    app = FastAPI(title="FleetSuite", version="0.1.0")

    # --- lifespan: own the db + services for the process lifetime ----------
    @app.on_event("startup")
    async def _startup() -> None:
        db = await Database(default_db_path()).connect()
        hub = Hub()
        hub.bind_loop(asyncio.get_running_loop())

        app.state.db = db
        app.state.hub = hub
        app.state.orch = builder.BuildOrchestrator(db, hub)
        app.state.serialmon = serial_monitor.SerialMonitor(db, hub)
        # Per-device port arbitration shared by every port-bound operation
        # (control actions, enrichment, keep-alive) so no two open one device.
        app.state.portlocks = portlock.PortLocks(serialmon=app.state.serialmon)
        # Escalating troubleshooting ladder (reboot → power-cycle → reflash).
        app.state.recovery = recovery.RecoveryService(
            db, hub, serialmon=app.state.serialmon, portlocks=app.state.portlocks
        )
        # The runner frees all device ports (suspends serial monitors) before
        # launching pytest, so it needs the monitor handle.
        app.state.runner = test_runner.TestRunner(db, hub, serialmon=app.state.serialmon)
        # Datadog forwarder (FleetLog): ships captured device logs continuously
        # and drives fleet-wide capture via the serial monitor.
        app.state.forwarder = datadog.DDForwarder(db, hub, serialmon=app.state.serialmon)
        app.state.serialmon.forwarder = app.state.forwarder  # lines → forwarder
        await app.state.forwarder.reload()
        await app.state.forwarder.start()
        # Discovery auto-enriches devices (suspending their serial monitor for the
        # connect) and keeps the fleet-log capture in sync with what's online.
        app.state.discovery = discovery.DeviceDiscovery(
            db,
            hub,
            serialmon=app.state.serialmon,
            forwarder=app.state.forwarder,
            portlocks=app.state.portlocks,
        )
        app.state.discovery.start()
        # Screen keep-alive: provision + periodically poke device displays so the
        # cameras always have a lit screen to watch.
        app.state.keepalive = keepalive.ScreenKeepAlive(
            db, hub, serialmon=app.state.serialmon, portlocks=app.state.portlocks
        )
        await app.state.keepalive.reload()
        await app.state.keepalive.start()
        log.info("FleetSuite started — registry at %s", db.path)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        disc = getattr(app.state, "discovery", None)
        if disc:
            await disc.stop()
        ka = getattr(app.state, "keepalive", None)
        if ka:
            await ka.stop()
        fwd = getattr(app.state, "forwarder", None)
        if fwd:
            await fwd.stop()
        sm = getattr(app.state, "serialmon", None)
        if sm:
            await sm.shutdown()
        db = getattr(app.state, "db", None)
        if db:
            await db.close()

    api = APIRouter(prefix="/api")
    _mount_devices(api)
    _mount_cameras(api)
    _mount_firmware(api)
    _mount_builds(api)
    _mount_datadog(api)
    _mount_tests(api)
    _mount_native(api)
    _mount_boards(api)
    _mount_hubs(api)
    _mount_keepalive(api)
    _mount_debug(api)
    app.include_router(api)
    _mount_ws(app)

    @app.exception_handler(ControlBusy)
    async def _busy(_req: Request, exc: ControlBusy):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ConfigError)
    async def _config_precondition(_req: Request, exc: ConfigError):
        # A missing/invalid firmware checkout is a config precondition, not a
        # server bug — any firmware-dependent lookup that raises ConfigError
        # answers 409 + the message app-wide instead of a raw 500 traceback.
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="spa")
    else:
        log.warning("no built SPA at %s — run `npm run build` in web-ui/", STATIC_DIR)

    return app


# --- helpers ---------------------------------------------------------------
async def _device_or_404(db: Database, serial: str) -> dict:
    row = await rd.get(db, serial)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown device: {serial}")
    return row


def _gate_idle() -> None:
    try:
        control._ensure_idle()
    except ControlBusy as exc:
        raise _busy_guard(exc)


# Identify tuning: how long to wait for a node to drop after cutting its port,
# and how long to let things settle before the next candidate.
_IDENTIFY_DROP_TIMEOUT = 9.0


async def _identify_slot(db: Database, location: str, port: int) -> str | None:
    """Cut power to one hub slot, watch which online device drops, restore power.
    Returns the serial that went offline (the device on that slot), or None."""
    loop = asyncio.get_running_loop()
    before = {d["serial_number"] for d in await rd.list_all(db) if d.get("online")}
    await asyncio.to_thread(power.power_slot, location, port, "off")
    dropped: str | None = None
    try:
        deadline = loop.time() + _IDENTIFY_DROP_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            offline_now = {d["serial_number"] for d in await rd.list_all(db) if not d.get("online")}
            hit = before & offline_now
            if hit:
                dropped = sorted(hit)[0]
                break
    finally:
        # Always restore power, even if detection failed or errored.
        await asyncio.to_thread(power.power_slot, location, port, "on")
    return dropped


async def _port_action(
    request: Request,
    serial: str,
    fn,
    *args,
    kind: str | None = None,
    wants_lines: bool = False,
    cb_factory=None,
    auto_recover: bool = False,
    recover_port: str | None = None,
):
    """Run a blocking port-bound library call under the device's port guard —
    exclusive against enrichment / keep-alive / other control actions, with the
    serial monitor suspended for the duration.

    When ``kind`` is set, the call is wrapped in an :class:`Activity` so the UI
    gets a server-backed progress stream (ticking elapsed + start/done) for
    free. ``wants_lines`` wires ``act.line`` in as the action's ``progress_cb``
    (each forwarded line refreshes the last output line); ``cb_factory(act)``
    builds a richer per-line callback bound to the live activity (flash uses it
    to filter pio output + derive a coarse phase).

    When ``auto_recover`` is set with ``recover_port``, the port is pre-flighted
    inside the guard: if it won't even open non-exclusively (a leaked fd holds
    it, or the device wedged), :func:`port_recovery.ensure_port_free` frees it
    first so reboot/config self-heal a stale port instead of failing. The
    power-cycle escalation is suppressed while a test run owns the ports."""
    _gate_idle()
    async with request.app.state.portlocks.guard(serial):
        if auto_recover and recover_port:
            await _ensure_openable(recover_port)
        if kind is None:
            return await asyncio.to_thread(fn, *args)
        async with Activity(request.app.state.hub, kind, serial) as act:
            if cb_factory is not None:
                return await asyncio.to_thread(fn, *args, progress_cb=cb_factory(act))
            if wants_lines:
                return await asyncio.to_thread(fn, *args, progress_cb=act.line)
            return await asyncio.to_thread(fn, *args)


async def _ensure_openable(port: str) -> None:
    """Best-effort self-heal of a stale port before a control action. Only
    escalates when even a *non-exclusive* open fails (a genuine wedge / lost
    holder, not our own monitor): then :func:`port_recovery.ensure_port_free`
    waits it out and, if a test run isn't holding the bus, power-cycles the
    device's own hub slot. Failure is swallowed — the action itself surfaces a
    clear error if the port is still unusable."""
    ok, _ = await asyncio.to_thread(port_recovery.port_openable, port, exclusive=False)
    if ok:
        return
    try:
        await asyncio.to_thread(
            port_recovery.ensure_port_free,
            port,
            allow_power_cycle=not test_runner.is_running(),
        )
    except port_recovery.PortRecoveryError as exc:
        log.warning("auto-recover of %s before control action failed: %s", port, exc)


def _flash_phase(line: str) -> str | None:
    """Map a pio/esptool output line to a coarse flash phase, or None. The
    compile half emits Compiling/Linking/…; the upload half emits
    Uploading/Writing at/… (which builder._is_progress doesn't recognise, so
    the phase doubles as the 'forward this line' signal for upload output)."""
    if line.startswith(("Compiling", "Linking", "Archiving", "Building", "Indexing")):
        return "compiling"
    if line.startswith(("Uploading", "Writing at", "Hash of data", "Leaving", "Wrote")):
        return "uploading"
    return None


def _flash_line_cb(act: Activity):
    """Build flash's per-line progress callback bound to ``act``: forward only
    lines that read as progress (builder._is_progress, or an upload-phase line),
    deriving the coarse compiling/uploading phase as it goes."""

    def on_line(line: str) -> None:
        phase = _flash_phase(line)
        if phase is None and not builder._is_progress(line):
            return
        if phase:
            act.phase(phase)
        act.line(line.strip())

    return on_line


# --- devices ---------------------------------------------------------------
def _mount_devices(api: APIRouter) -> None:
    @api.get("/devices")
    async def list_devices(request: Request):
        return await rd.list_all(request.app.state.db)

    @api.patch("/devices/{serial}")
    async def patch_device(serial: str, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        if "friendly_name" in body:
            dev = await rd.set_friendly_name(db, serial, body["friendly_name"])
        else:
            dev = await rd.get(db, serial)
        await hub.publish("device.update", dev)
        return dev

    @api.put("/devices/{serial}/env")
    async def set_env(serial: str, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        env = body.get("env")
        # A provided env pins it; clearing it releases the pin to auto-detect.
        dev = await rd.set_env(db, serial, env, locked=env is not None)
        await hub.publish("device.update", dev)
        return dev

    @api.post("/devices/{serial}/refresh")
    async def refresh(serial: str, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        row = await _device_or_404(db, serial)
        port = row.get("current_port")
        info = await _port_action(request, serial, mt_info.device_info, port)
        hw_model = info.get("hw_model")
        env = identity.env_for_hw_model(hw_model) if hw_model else None
        dev = await rd.update_enrichment(
            db,
            serial,
            node_num=info.get("my_node_num"),
            env=env,
            hw_model=hw_model,
            firmware_version=info.get("firmware_version"),
            region=info.get("region"),
        )
        await hub.publish("device.update", dev)
        return {"device": dev}

    @api.get("/devices/{serial}/flash-stats")
    async def flash_stats(serial: str, request: Request):
        return await rf.comparison(request.app.state.db, serial)

    @api.post("/devices/{serial}/flash")
    async def flash_device(serial: str, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        row = await _device_or_404(db, serial)
        env = control.env_for_device(row)
        port = row.get("current_port")
        if not env or not port:
            raise HTTPException(status_code=400, detail="no env/port resolved")
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await _port_action(
            request,
            serial,
            lambda progress_cb=None: flash_lib.flash(
                env, port, confirm=True, progress_cb=progress_cb
            ),
            kind="flash",
            cb_factory=_flash_line_cb,
        )
        duration = round(loop.time() - start, 2)
        ok = result.get("exit_code") == 0
        fw = firmware.firmware_ref()
        await rf.record(
            db,
            device_serial=serial,
            env=env,
            fw_sha=fw.get("sha"),
            from_artifact=False,
            duration_s=duration,
            ok=ok,
        )
        if ok:
            await rd.record_flashed(db, serial, branch=fw.get("branch"), sha=fw.get("sha"))
            await hub.publish("device.update", await rd.get(db, serial))
        return {"ok": ok, "duration_s": duration, **result}

    @api.post("/devices/{serial}/reboot")
    async def reboot(serial: str, request: Request):
        row = await _device_or_404(request.app.state.db, serial)
        port = row.get("current_port")
        return await _port_action(
            request,
            serial,
            admin.reboot,
            port,
            True,
            5,
            kind="reboot",
            auto_recover=True,
            recover_port=port,
        )

    @api.post("/devices/{serial}/factory-reset")
    async def factory_reset(serial: str, request: Request):
        row = await _device_or_404(request.app.state.db, serial)
        return await _port_action(
            request,
            serial,
            admin.factory_reset,
            row.get("current_port"),
            True,
            kind="factory-reset",
        )

    @api.post("/devices/{serial}/send-text")
    async def send_text(serial: str, request: Request, body: dict = Body(...)):
        row = await _device_or_404(request.app.state.db, serial)
        text = body.get("text", "")
        return await _port_action(
            request, serial, admin.send_text, text, None, 0, False, row.get("current_port")
        )

    @api.post("/devices/{serial}/inject-nodedb")
    async def inject_nodedb(serial: str, request: Request, body: dict = Body(...)):
        row = await _device_or_404(request.app.state.db, serial)
        size = int(body.get("size", 500))
        return await _port_action(
            request,
            serial,
            _inject,
            size,
            row.get("current_port"),
            kind="inject-nodedb",
            wants_lines=True,
        )

    @api.get("/devices/{serial}/config")
    async def get_config(serial: str, request: Request, section: str | None = None):
        row = await _device_or_404(request.app.state.db, serial)
        port = row.get("current_port")
        return await _port_action(
            request,
            serial,
            admin.get_config,
            section,
            port,
            auto_recover=True,
            recover_port=port,
        )

    @api.put("/devices/{serial}/config")
    async def set_config(serial: str, request: Request, body: dict = Body(...)):
        row = await _device_or_404(request.app.state.db, serial)
        path = body.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="missing config path")
        port = row.get("current_port")
        return await _port_action(
            request,
            serial,
            admin.set_config,
            path,
            body.get("value"),
            port,
            auto_recover=True,
            recover_port=port,
        )

    @api.get("/devices/{serial}/packets")
    async def device_packets(serial: str, request: Request, start: str = "-30m", max: int = 100):
        await _device_or_404(request.app.state.db, serial)
        # Recorder packets are mesh-wide, not keyed by USB port — return the
        # recent window so the per-device tab has live traffic to show.
        window = await asyncio.to_thread(lambda: log_query.packets_window(start, "now", max=max))
        return {"packets": window.get("packets", [])}

    @api.get("/devices/{serial}/test-results")
    async def device_test_results(serial: str, request: Request, limit: int = 100):
        rows = await rr.results_for_device(request.app.state.db, serial)
        return rows[:limit]

    @api.post("/devices/{serial}/recover")
    async def recover_device(serial: str, request: Request, body: dict = Body(default={})):
        """Run the escalating recovery ladder (reboot → power-cycle, plus
        1200bps→reflash when allow_reflash — destructive, so it also requires
        confirm). Long-running; streams progress on the recovery.update WS topic."""
        await _device_or_404(request.app.state.db, serial)
        _gate_idle()
        try:
            return await request.app.state.recovery.recover(
                serial,
                allow_reflash=bool(body.get("allow_reflash")),
                confirm=bool(body.get("confirm")),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @api.post("/devices/{serial}/unwedge")
    async def unwedge_device(serial: str, request: Request):
        """Lightweight sibling to /recover: just free the serial port. Waits out a
        transient holder and, if the device is genuinely wedged (hung firmware /
        stale CDC node), power-cycles its OWN hub slot (resolved from USB
        topology, correct on a bench of same-VID boards) to re-enumerate it.
        Returns ``{recovered, new_port, holders}``; the port may come back on a
        new path. Gated idle so it never cycles mid-run."""
        db, hub = request.app.state.db, request.app.state.hub
        row = await _device_or_404(db, serial)
        if row.get("kind") == "native":
            raise HTTPException(
                status_code=400, detail="native (TCP) nodes have no serial port to unwedge"
            )
        port = row.get("current_port")
        if not port:
            raise HTTPException(status_code=400, detail="no current_port for this device")
        _gate_idle()
        async with request.app.state.portlocks.guard(serial):
            holders = await asyncio.to_thread(port_recovery.who_holds_port, port)
            try:
                # Re-check the runner here (not just _gate_idle above): a run may
                # have started while we waited on the port guard.
                new_port = await asyncio.to_thread(
                    port_recovery.ensure_port_free,
                    port,
                    allow_power_cycle=not test_runner.is_running(),
                )
            except port_recovery.PortRecoveryError as exc:
                await hub.publish("device.update", {**row, "note": f"unwedge failed: {exc}"})
                return {
                    "recovered": False,
                    "new_port": None,
                    "holders": holders,
                    "error": str(exc),
                }
        note = "unwedged: recovered on " + new_port + (f" (was {port})" if new_port != port else "")
        await hub.publish("device.update", {**row, "current_port": new_port, "note": note})
        return {"recovered": True, "new_port": new_port, "holders": holders}

    # --- per-device USB power (uhubctl) ----------------------------------
    @api.put("/devices/{serial}/hub-port")
    async def set_hub_port(serial: str, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        loc = body.get("location")
        port = body.get("port")
        dev = await rd.set_hub_port(
            db, serial, location=loc, port=int(port) if port is not None else None
        )
        await hub.publish("device.update", dev)
        return dev

    @api.post("/devices/{serial}/locate")
    async def locate_device(serial: str, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        try:
            res = await power.locate(db, serial)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if res["located"]:
            await hub.publish("device.update", res["device"])
        return res

    @api.post("/devices/{serial}/identify")
    async def identify_device(serial: str, request: Request):
        """Map this device (and any same-VID siblings it cycles) to hub ports by
        cutting power slot-by-slot and watching which node drops. Auto-pins each
        match. Long-running — it briefly power-cycles candidate ports."""
        db, hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        _gate_idle()
        if not power.available():
            raise HTTPException(status_code=400, detail="uhubctl not available on this host")
        dev = await rd.get(db, serial)
        candidates = await asyncio.to_thread(power.candidates_for, dev)
        if not candidates:
            raise HTTPException(
                status_code=400, detail="no PPPS hub port matches this device's VID"
            )

        disc = request.app.state.serialmon, request.app.state.discovery
        sm, discovery_svc = disc
        prev_enrich = discovery_svc.auto_enrich
        discovery_svc.auto_enrich = False  # don't connect to devices we're cycling
        await sm.suspend_all()

        # Ports already bound to a device — don't disturb known mappings.
        pinned: dict[tuple, str] = {}
        for d in await rd.list_all(db):
            if d.get("hub_location") is not None and d.get("hub_port") is not None:
                pinned[(d["hub_location"], d["hub_port"])] = d["serial_number"]

        found: dict | None = None
        mapped: list[dict] = []
        try:
            for c in candidates:
                key = (c["location"], c["port"])
                if key in pinned:
                    if pinned[key] == serial:
                        found = c
                    continue
                dropped = await _identify_slot(db, c["location"], c["port"])
                if dropped:
                    updated = await rd.set_hub_port(
                        db, dropped, location=c["location"], port=c["port"]
                    )
                    await hub.publish("device.update", updated)
                    pinned[key] = dropped
                    mapped.append({"serial": dropped, **c})
                    if dropped == serial:
                        found = c
                        break
        except RuntimeError as exc:  # uhubctl failed mid-sweep
            raise HTTPException(status_code=502, detail=str(exc))
        finally:
            discovery_svc.auto_enrich = prev_enrich
            await sm.resume_all()

        return {
            "identified": found is not None,
            "slot": found,
            "mapped": mapped,
            "device": await rd.get(db, serial),
        }

    @api.post("/devices/{serial}/power/{action}")
    async def power_action(serial: str, action: str, request: Request):
        db, _hub = request.app.state.db, request.app.state.hub
        await _device_or_404(db, serial)
        if action not in ("on", "off", "cycle"):
            raise HTTPException(status_code=404, detail="unknown power action")
        _gate_idle()
        # The guard serialises against enrichment/keep-alive (which open the
        # port under the same lock) and suspends any live serial monitor while
        # VBUS is toggled, resuming it on exit even when the action fails.
        async with request.app.state.portlocks.guard(serial):
            try:
                result = await power.power_device(db, serial, action)
            except AmbiguousPort as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"error": str(exc), "candidates": exc.candidates},
                )
            except (NoPort, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except RuntimeError as exc:  # uhubctl errors (permissions, hub gone)
                raise HTTPException(status_code=502, detail=str(exc))
        return result


def _inject(size: int, port: str | None, progress_cb=None) -> dict:
    return fixtures.push_fake_nodedb(
        size,
        target="hardware",
        port=port,
        confirm=True,
        reboot_after=True,
        progress_cb=progress_cb,
    )


# --- cameras ---------------------------------------------------------------
def _mount_cameras(api: APIRouter) -> None:
    @api.get("/cameras")
    async def list_cameras(request: Request):
        return await rc.list_all(request.app.state.db)

    @api.get("/cameras/discover")
    async def discover_cameras(request: Request):
        # Indices already bound to a FleetSuite camera — don't re-open those.
        in_use = {
            str(c["device_index"])
            for c in await rc.list_all(request.app.state.db)
            if c.get("device_index") is not None
        }
        return await asyncio.to_thread(camera_stream.discover, in_use)

    @api.post("/cameras")
    async def add_camera(request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        cid = await rc.add(
            db, name=body.get("name", "camera"), device_index=str(body.get("device_index", "0"))
        )
        cam = await rc.get(db, cid)
        await hub.publish("camera.update", cam)
        return cam

    @api.delete("/cameras/{cid}", status_code=204)
    async def remove_camera(cid: int, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        await rc.remove(db, cid)
        await hub.publish("camera.update", {"id": cid, "deleted": True})

    @api.post("/cameras/{cid}/assign")
    async def assign_camera(cid: int, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        cam = await rc.assign(db, cid, body.get("device_serial"))
        if cam is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        await hub.publish("camera.update", cam)
        return cam

    @api.post("/cameras/{cid}/rotation")
    async def rotate_camera(cid: int, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        cam = await rc.set_rotation(db, cid, int(body.get("rotation", 0)))
        if cam is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        await hub.publish("camera.update", cam)
        return cam

    @api.post("/cameras/{cid}/mirror")
    async def mirror_camera(cid: int, request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        cam = await rc.set_mirror(db, cid, bool(body.get("mirror", False)))
        if cam is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        await hub.publish("camera.update", cam)
        return cam

    @api.get("/cameras/{cid}/status")
    async def camera_status(cid: int, request: Request):
        cam = await rc.get(request.app.state.db, cid)
        if cam is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        return await asyncio.to_thread(camera_stream.probe, str(cam.get("device_index")))

    @api.get("/cameras/{cid}/stream.mjpg")
    async def camera_stream_ep(cid: int, request: Request):
        cam = await rc.get(request.app.state.db, cid)
        if cam is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        probe = await asyncio.to_thread(camera_stream.probe, str(cam.get("device_index")))
        if not probe["ok"]:
            raise HTTPException(status_code=503, detail=probe["error"])
        return StreamingResponse(
            camera_stream.mjpeg(str(cam.get("device_index"))),
            media_type=f"multipart/x-mixed-replace; boundary={camera_stream.BOUNDARY}",
        )


# --- firmware --------------------------------------------------------------
def _mount_firmware(api: APIRouter) -> None:
    @api.get("/firmware")
    async def get_firmware():
        return await asyncio.to_thread(firmware.firmware_ref)


# --- builds ----------------------------------------------------------------
def _mount_builds(api: APIRouter) -> None:
    @api.get("/builds")
    async def list_builds(request: Request):
        db = request.app.state.db
        return {
            "docker": await asyncio.to_thread(builder.docker_available),
            "builds": await rb.list_all(db),
        }

    @api.post("/builds")
    async def enqueue_builds(request: Request, body: dict = Body(default={})):
        db = request.app.state.db
        orch = request.app.state.orch
        fw = await asyncio.to_thread(firmware.firmware_ref)
        if not fw.get("available"):
            raise HTTPException(status_code=400, detail="no firmware checkout")
        sha, branch = fw["sha"], fw.get("branch")

        envs = body.get("envs")
        if not envs:
            # Prebuild the envs every online, env-resolved device needs.
            envs = sorted(
                {control.env_for_device(d) for d in await rd.online_with_env(db)} - {None}
            )
        if not envs:
            return []
        return await orch.enqueue(list(envs), sha=sha, branch=branch, force=bool(body.get("force")))


# --- datadog ---------------------------------------------------------------
def _mount_datadog(api: APIRouter) -> None:
    @api.get("/datadog")
    async def get_datadog(request: Request):
        return request.app.state.forwarder.status()

    @api.put("/datadog")
    async def put_datadog(request: Request, body: dict = Body(...)):
        db = request.app.state.db
        fwd = request.app.state.forwarder
        cfg = await datadog.load_config(db)
        # The only operator-editable field is the tester id; everything else
        # (US5 site, redact scrub, collector, ship_debug) is baked. Shipping is
        # always on once a token is present.
        if "host" in body:
            cfg.host = body["host"]
        # Only overwrite the key if a (non-empty) one was supplied.
        if body.get("api_key"):
            cfg.api_key = body["api_key"]
        await datadog.save_config(db, cfg)
        await fwd.reload()
        status = fwd.status()
        await request.app.state.hub.publish("datadog.update", status)
        return status

    @api.post("/datadog/test")
    async def test_datadog(request: Request):
        fwd = request.app.state.forwarder
        await fwd.reload()
        return await asyncio.to_thread(fwd.test_key)


# --- tests -----------------------------------------------------------------
def _mount_tests(api: APIRouter) -> None:
    @api.get("/tests/status")
    async def tests_status():
        return test_runner.status()

    @api.get("/tests/runs")
    async def tests_runs(request: Request):
        return await rr.list_runs(request.app.state.db)

    @api.post("/tests/start")
    async def tests_start(request: Request, body: dict = Body(default={})):
        runner = request.app.state.runner
        try:
            return await runner.start(list(body.get("args", [])))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @api.post("/tests/stop", status_code=204)
    async def tests_stop(request: Request):
        await request.app.state.runner.stop()

    @api.post("/tests/force-reset")
    async def tests_force_reset(request: Request):
        """Clear a wedged run without restarting the server: cancel the drive
        task (its finally restores state + serial monitors) and hard-reset the
        run flag as a fallback."""
        await request.app.state.runner.reset()
        return test_runner.status()


# --- debug -----------------------------------------------------------------
def _mount_debug(api: APIRouter) -> None:
    @api.get("/debug/tasks")
    async def debug_tasks():
        """Dump every live asyncio task and its stack. The only way to see where
        a *parked* coroutine (e.g. a wedged test run) is actually waiting — it
        isn't on any OS-thread stack, so py-spy/sample/lldb can't show it."""

        out = []
        for task in asyncio.all_tasks():
            frames = task.get_stack()
            stack = [f"{f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}" for f in frames]
            out.append(
                {
                    "name": task.get_name(),
                    "done": task.done(),
                    "coro": repr(task.get_coro()),
                    "stack": stack,
                }
            )
        return out


# --- native ----------------------------------------------------------------
def _mount_native(api: APIRouter) -> None:
    @api.get("/native")
    async def native_info(request: Request):
        return await native.info(request.app.state.db)

    @api.post("/native")
    async def native_create(request: Request, body: dict = Body(...)):
        db, hub = request.app.state.db, request.app.state.hub
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="missing name")
        try:
            dev = await native.create(db, name=name, tcp_port=int(body.get("tcp_port", 4403)))
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await hub.publish("device.update", dev)
        return dev

    @api.post("/native/{name}/{action}")
    async def native_lifecycle(name: str, action: str, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=404, detail="unknown action")
        try:
            dev = await native.lifecycle(db, name, action)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await hub.publish("device.update", dev)
        return dev

    @api.delete("/native/{name}", status_code=204)
    async def native_delete(name: str, request: Request):
        db, hub = request.app.state.db, request.app.state.hub
        await native.remove(db, name)
        await hub.publish("device.update", {"serial_number": f"native:{name}", "deleted": True})


# --- boards ----------------------------------------------------------------
def _mount_boards(api: APIRouter) -> None:
    @api.get("/boards")
    async def list_boards(query: str | None = None, architecture: str | None = None):
        # ConfigError (no firmware checkout) → 409 via the app-wide handler.
        return await asyncio.to_thread(boards.list_boards, architecture, False, query, None)


# --- screen keep-alive -----------------------------------------------------
def _mount_keepalive(api: APIRouter) -> None:
    @api.get("/keepalive")
    async def keepalive_status(request: Request):
        return request.app.state.keepalive.status()

    @api.put("/keepalive")
    async def keepalive_save(request: Request, body: dict = Body(...)):
        ka = request.app.state.keepalive
        await ka.save(body)
        status = ka.status()
        await request.app.state.hub.publish("keepalive.update", status)
        return status


# --- hubs (uhubctl) --------------------------------------------------------
def _mount_hubs(api: APIRouter) -> None:
    @api.get("/hubs")
    async def list_hubs():
        if not power.available():
            return {"available": False, "hubs": []}
        try:
            hubs = await asyncio.to_thread(power.list_hubs)
        except RuntimeError as exc:  # uhubctl present but failed (permissions)
            return {"available": True, "hubs": [], "error": str(exc)}
        return {"available": True, "hubs": hubs}


# --- websocket -------------------------------------------------------------
def _mount_ws(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub: Hub = websocket.app.state.hub
        sm = websocket.app.state.serialmon
        conn = Connection(send=websocket.send_json)
        hub.add(conn)
        # Track this peer's live serial monitors so we can release them on drop.
        serials: set[str] = set()
        try:
            while True:
                msg = await websocket.receive_json()
                action = msg.get("action")
                topic = msg.get("topic")
                if action == "subscribe" and topic:
                    hub.subscribe(conn, topic)
                    if topic.startswith("serial.") and topic not in serials:
                        serials.add(topic)
                        await sm.acquire(topic[len("serial.") :])
                elif action == "unsubscribe" and topic:
                    hub.unsubscribe(conn, topic)
                    if topic in serials:
                        serials.discard(topic)
                        await sm.release(topic[len("serial.") :])
        except Exception:
            pass
        finally:
            hub.remove(conn)
            for topic in serials:
                await sm.release(topic[len("serial.") :])
