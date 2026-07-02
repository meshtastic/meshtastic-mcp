# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The server-side activity stream for device actions.

Covers the :class:`Activity` context manager (started → heartbeat → done/error
frames, plus the worker-thread .line/.phase callbacks) and flash's per-line
progress filter that derives a coarse compiling/uploading phase.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")  # optional extra — a bare [test] install skips these
pytest.importorskip("aiosqlite")  # optional extra — a bare [test] install skips these

from meshtastic_mcp.web import app as webapp
from meshtastic_mcp.web.services import activity
from meshtastic_mcp.web.services.activity import Activity
from meshtastic_mcp.web.ws.hub import Hub


class _CapHub(Hub):
    """A Hub that records every published (topic, data) frame, even with no
    connections, so a test can assert which topic carried what."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, object]] = []

    async def publish(self, topic, data):  # type: ignore[override]
        self.frames.append((topic, dict(data) if isinstance(data, dict) else data))
        await super().publish(topic, data)


def test_activity_emits_started_heartbeat_done(monkeypatch):
    # Tight heartbeat so a sub-second test sees several beats.
    monkeypatch.setattr(activity, "HEARTBEAT_S", 0.03)

    async def go():
        hub = _CapHub()
        hub.bind_loop(asyncio.get_running_loop())
        async with Activity(hub, "flash", "/dev/cu.x") as act:
            assert act.id.startswith("flash:/dev/cu.x:")
            await asyncio.sleep(0.12)  # let a few heartbeats fire

        states = [d["state"] for t, d in hub.frames if t == "action.update"]
        # Every frame rides the action.update topic and carries the full shape.
        for t, d in hub.frames:
            assert t == "action.update"
            assert set(d) == {
                "id",
                "kind",
                "target",
                "phase",
                "state",
                "elapsed_s",
                "last_line",
                "ts",
            }
        assert states[0] == "started"
        assert states[-1] == "done"
        assert states.count("running") >= 2  # heartbeat fired repeatedly
        # Elapsed is monotonic non-decreasing across the stream.
        elapsed = [d["elapsed_s"] for _, d in hub.frames]
        assert elapsed == sorted(elapsed)

    asyncio.run(go())


def test_activity_error_state_and_reraises(monkeypatch):
    monkeypatch.setattr(activity, "HEARTBEAT_S", 100.0)  # no heartbeat noise

    async def go():
        hub = _CapHub()
        hub.bind_loop(asyncio.get_running_loop())
        with pytest.raises(ValueError):
            async with Activity(hub, "reboot", "abc"):
                raise ValueError("boom")  # body failed

        states = [d["state"] for t, d in hub.frames if t == "action.update"]
        assert states == ["started", "error"]  # error finalised, not done

    asyncio.run(go())


def test_activity_line_and_phase(monkeypatch):
    monkeypatch.setattr(activity, "HEARTBEAT_S", 100.0)  # isolate line/phase frames

    async def go():
        hub = _CapHub()
        hub.bind_loop(asyncio.get_running_loop())
        async with Activity(hub, "inject-nodedb", "node1") as act:
            act.phase("compiling proto")
            act.line("xmodem 3/40")
            await asyncio.sleep(0.05)  # drain threadsafe-scheduled publishes

        frames = [d for t, d in hub.frames if t == "action.update"]
        running = [d for d in frames if d["state"] == "running"]
        assert running, "line/phase should emit running frames"
        last = running[-1]
        assert last["phase"] == "compiling proto"
        assert last["last_line"] == "xmodem 3/40"

    asyncio.run(go())


class _FakeAct:
    """Records phase/line calls so the flash filter can be tested without a hub."""

    def __init__(self) -> None:
        self.phases: list[str] = []
        self.lines: list[str] = []

    def phase(self, p: str) -> None:
        self.phases.append(p)

    def line(self, s: str) -> None:
        self.lines.append(s)


def test_flash_line_cb_filters_and_phases():
    act = _FakeAct()
    on_line = webapp._flash_line_cb(act)

    on_line("Compiling .pio/build/heltec-v3/src/main.cpp.o")  # compile progress
    on_line("-I/Users/x/.platformio/packages/framework/include")  # noise, dropped
    on_line("Linking .pio/build/heltec-v3/firmware.elf")  # compile progress
    on_line("Uploading .pio/build/heltec-v3/firmware.bin")  # upload phase
    on_line("Writing at 0x00010000... (12 %)")  # upload progress (not _is_progress)
    on_line("RAM:   [===       ]  34.2%")  # _is_progress, no phase

    # The -I include flag is filtered out; everything else is forwarded.
    assert act.lines == [
        "Compiling .pio/build/heltec-v3/src/main.cpp.o",
        "Linking .pio/build/heltec-v3/firmware.elf",
        "Uploading .pio/build/heltec-v3/firmware.bin",
        "Writing at 0x00010000... (12 %)",
        "RAM:   [===       ]  34.2%",
    ]
    # Phase advances compile → upload as the prefixes change.
    assert act.phases == ["compiling", "compiling", "uploading", "uploading"]


@pytest.mark.firmware
def test_flash_forwards_filtered_lines_through_pio(monkeypatch):
    """flash.flash() wires progress_cb straight into pio.run's line_cb, so a
    streamed pio line reaches the caller's callback."""
    from meshtastic_mcp import flash as flash_lib
    from meshtastic_mcp import pio, port_recovery

    seen: list[str] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""
        duration_s = 1.0

    def fake_pio_run(args, *, line_cb=None, **kw):
        # Emulate pio streaming compile + upload output through line_cb.
        for ln in ("Compiling foo.cpp", "noise -DBAR", "Writing at 0x1000 (5 %)"):
            if line_cb:
                line_cb(ln)
        return _Result()

    monkeypatch.setattr(pio, "run", fake_pio_run)
    # flash() pre-flights the port through port_recovery; stub it so the fake
    # /dev path is treated as already usable (no hardware here).
    monkeypatch.setattr(port_recovery, "ensure_port_free", lambda port, **kw: port)
    flash_lib.flash("heltec-v3", "/dev/cu.x", confirm=True, progress_cb=seen.append)
    assert seen == ["Compiling foo.cpp", "noise -DBAR", "Writing at 0x1000 (5 %)"]


def test_inject_forwards_progress_cb_through_push_fake_nodedb(monkeypatch, tmp_path):
    """The inject-nodedb endpoint runs webapp._inject with wants_lines=True, so
    _port_action always passes progress_cb=act.line — the kwarg must survive the
    whole real-signature chain (_inject → push_fake_nodedb → _push_hardware) or
    every POST /devices/{serial}/inject-nodedb dies with a TypeError."""
    import inspect

    from meshtastic_mcp import fixtures

    # The real hardware uploader accepts progress_cb (second hop of the chain).
    assert "progress_cb" in inspect.signature(fixtures._push_hardware).parameters

    seed = tmp_path / "seed_v25_0500.jsonl"
    seed.write_text("{}\n")
    monkeypatch.setattr(fixtures, "_resolve_seed_jsonl", lambda size, custom: seed)

    seen: list[str] = []

    def fake_push_hardware(size, jsonl, port, reboot_after, progress_cb=None):
        # Emulate the real uploader's coarse progress stream.
        for msg in ("compiling proto", "xmodem 1/40", "rebooting"):
            if progress_cb:
                progress_cb(msg)
        return {"transport": "hardware", "port": port, "bytes": 0}

    monkeypatch.setattr(fixtures, "_push_hardware", fake_push_hardware)

    # Through the REAL push_fake_nodedb signature — this is the call that used
    # to raise TypeError: push_fake_nodedb() got an unexpected keyword argument.
    out = webapp._inject(500, "/dev/cu.x", progress_cb=seen.append)

    assert out["transport"] == "hardware"
    assert seen == ["compiling proto", "xmodem 1/40", "rebooting"]
