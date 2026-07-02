#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Device-plane e2e for CI: spin up a virtual `meshtasticd` mesh and assert delivery.

The deterministic, hardware- and app-free half of the e2e lab. Builds N native nodes over
UDP multicast (`meshtastic_mcp.emulator.native_node`), supervises each under a restart loop
(so the admin-config reboot relaunches with persisted config — the gotcha called out in
`native_node`'s own docstring), warms up NodeInfo, then runs the **inbound loop over TCP**:
the tester node broadcasts a unique token and the DUT node must receive it as a
`TEXT_MESSAGE_APP`.

Emits a single grep-able verdict line (`LOOP inbound PASS|FAIL token=… latency=…ms`) and exits
non-zero on FAIL, so a CI step or an agent can branch on it. The Android/iOS *app* plane is the
separate, heavier leg (see `emulator-lab.md` / `simulator-apple.md`); this leg gives a fast,
reliable regression signal for the mesh + transport + admin path with no emulator flakiness.

    python scripts/ci_device_mesh_e2e.py --binary .pio/build/native/meshtasticd

Run it with an environment where `meshtastic` + `meshtastic_mcp` import (the package's own venv).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from meshtastic_mcp.emulator import native_node


def verdict(name: str, passed: bool, token: str, latency_ms: int | None, extra: str = "") -> str:
    """Format the one-line loop verdict (pure; unit-tested)."""
    status = "PASS" if passed else "FAIL"
    lat = f" latency={latency_ms}ms" if latency_ms is not None else ""
    tail = f" {extra}" if extra else ""
    return f"LOOP {name} {status} token={token!r}{lat}{tail}"


class _Supervisor:
    """Restart a node on exit (erase only on first launch) until stopped.

    `native_node.NativeNode.configure()` writes config via admin then the daemon reboots,
    which terminates the foreground process; the supervisor relaunches it (without `-e`) so it
    comes back up with the persisted region/UDP config.
    """

    def __init__(self, node: native_node.NativeNode) -> None:
        self.node = node
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sup-{node.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            self.node.start(erase=first)
            first = False
            proc = self.node._proc
            if proc is None:
                return
            while proc.poll() is None and not self._stop.is_set():
                time.sleep(0.5)
            if self._stop.is_set():
                return
            time.sleep(1)  # brief backoff before relaunch after a reboot

    def stop(self) -> None:
        self._stop.set()
        self.node.stop()


class _Listener:
    """Background TCP receiver on the DUT: records any TEXT_MESSAGE_APP carrying the token."""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self.hits: list[dict] = []
        self._iface = None

    def _on_text(self, packet=None, interface=None, **_):
        d = (packet or {}).get("decoded") or {}
        if d.get("portnum") != "TEXT_MESSAGE_APP":
            return
        payload = d.get("payload") or b""
        text = (
            payload.decode("utf-8", "replace")
            if isinstance(payload, (bytes, bytearray))
            else str(payload)
        )
        if self.token in text:
            self.hits.append({"from": packet.get("fromId"), "text": text})

    def __enter__(self) -> _Listener:
        import meshtastic.tcp_interface as tcp
        from pubsub import pub

        pub.subscribe(self._on_text, "meshtastic.receive.text")
        self._iface = tcp.TCPInterface("127.0.0.1", portNumber=self.port)
        return self

    def wait(self, timeout: float) -> tuple[bool, int]:
        t0 = time.time()
        while time.time() - t0 < timeout and not self.hits:
            time.sleep(0.5)
        return bool(self.hits), int((time.time() - t0) * 1000)

    def __exit__(self, *exc: object) -> None:
        from pubsub import pub

        try:
            if self._iface is not None:
                self._iface.close()
        except Exception:
            pass
        pub.unsubscribe(self._on_text, "meshtastic.receive.text")


def _broadcast(port: int, text: str) -> None:
    import meshtastic.tcp_interface as tcp

    iface = tcp.TCPInterface("127.0.0.1", portNumber=port)
    try:
        iface.sendText(text, wantAck=False)
        time.sleep(2)
    finally:
        try:
            iface.close()
        except Exception:
            pass


def firmware_version(port: int) -> str:
    """Best-effort provenance: the DUT's reported firmware version (or 'unknown').

    Lets a version-pinned run stamp its verdict with exactly what was tested.
    """
    import meshtastic.tcp_interface as tcp

    iface = tcp.TCPInterface("127.0.0.1", portNumber=port)
    try:
        meta = getattr(iface, "metadata", None)
        ver = getattr(meta, "firmware_version", None)
        return str(ver) if ver else "unknown"
    except Exception:
        return "unknown"
    finally:
        try:
            iface.close()
        except Exception:
            pass


@contextlib.contextmanager
def mesh_up(
    binary: Path, workdir: Path, count: int = 2
) -> Iterator[tuple[native_node.NativeNode, native_node.NativeNode]]:
    """Bring up an N-node virtual mesh, configured + warmed up; yield (dut, tester).

    Shared by the device-plane and emulator app-plane CI legs. Handles the supervised
    restart loop (for the config reboot), region+UDP admin config, and NodeInfo warmup.
    """
    nodes = native_node.build_lab(binary, workdir, count=count)
    dut, tester = nodes[0], nodes[-1]
    sups = [_Supervisor(n) for n in nodes]
    try:
        for s in sups:
            s.start()
        print(f"[ci-e2e] launched {count} nodes (DUT :{dut.tcp_port}, tester :{tester.tcp_port})")
        time.sleep(12)  # boot
        for n in nodes:
            n.configure()  # region + UDP via admin (triggers a reboot; supervisor relaunches)
        print("[ci-e2e] configured region + UDP; waiting for re-mesh")
        time.sleep(14)
        # Warm up NodeInfo both ways so the mesh is populated before any assertion.
        _broadcast(dut.tcp_port, "warmup-dut")
        _broadcast(tester.tcp_port, "warmup-tester")
        time.sleep(6)
        yield dut, tester
    finally:
        for s in sups:
            s.stop()


def run(binary: Path, workdir: Path, count: int, timeout: float) -> int:
    with mesh_up(binary, workdir, count=count) as (dut, tester):
        fw = firmware_version(dut.tcp_port)
        print(f"[ci-e2e] DUT firmware: {fw}")
        token = f"CI-E2E-{int(time.time())}"
        # Listen on the DUT first, then broadcast from the tester, then wait for the bubble.
        with _Listener(dut.tcp_port, token) as listener:
            time.sleep(1)  # let the subscription + connection settle
            _broadcast(tester.tcp_port, token)
            passed, latency = listener.wait(timeout)
            hit = listener.hits[0] if listener.hits else {}
        extra = f"fw={fw} {hit}"
        print(verdict("inbound", passed, token, latency if passed else None, extra=extra))
        return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CI device-plane mesh e2e (inbound loop over TCP)")
    p.add_argument("--binary", type=Path, required=True, help="path to the native meshtasticd")
    p.add_argument("--workdir", type=Path, default=Path("/tmp/ci-mesh-lab"))
    p.add_argument("--count", type=int, default=2)
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    if not args.binary.is_file():
        print(f"FAIL: meshtasticd binary not found at {args.binary}", file=sys.stderr)
        return 2
    return run(args.binary, args.workdir, args.count, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
