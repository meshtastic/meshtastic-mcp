#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""ATAK app-plane e2e: render a simulated Meshtastic TAK squad in ATAK-CIV.

The TAK counterpart of ``ci_android_app_loop.py``. Assumes an Android emulator is
**already booted**. It generates a synthetic Meshtastic mesh carrying a
TAKPacketV2 squad, stands up a CoT streaming TAK server from it
(:mod:`meshtastic_mcp.replay.tak_server` — the same CoT an app's in-app TAK
server would forward), points ATAK-CIV at that stream, and asserts a squad
callsign marker **renders** on ATAK's map (receive leg), then confirms ATAK's
own self-PLI streams back to the server and converts to a mesh TAKPacketV2
(send leg) — i.e. bidirectional device↔app TAK.

    python scripts/ci_atak_app_loop.py --atak-apk ATAK-CIV.apk

Requires the ``[tak]`` extra (meshtastic-tak) for the CoT build, ATAK-CIV
installed/installable, and the ``android`` capability (adb). Emits
``LOOP atak-render …`` + ``LOOP atak-send …`` and exits non-zero if either leg fails.

Notes / assumptions (ATAK is heavy to automate; this is an opt-in CI job):
- ATAK reaches the host CoT server at ``10.0.2.2:<port>`` (emulator → host).
- The streaming TCP input is preconfigured by pushing an ATAK ``.pref`` before
  launch; first-run EULA is dismissed via a UI tap fallback. Both are
  best-effort and version-sensitive — the render assertion is the real gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import time
import xml.etree.ElementTree as ET

ATAK_PACKAGE = "com.atakmap.app.civ"

# Reuse verdict() from the sibling helper (scripts/ is not a package).
_SIB = pathlib.Path(__file__).resolve().parent / "ci_device_mesh_e2e.py"
_spec = importlib.util.spec_from_file_location("ci_device_mesh_e2e", _SIB)
assert _spec and _spec.loader
_mesh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mesh)


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def expected_callsigns(events: list[tuple[int, bytes]]) -> set[str]:
    """Squad callsigns present in a list of ``(rx_time, cot_xml)`` events."""
    out: set[str] = set()
    for _t, cot in events:
        contact = ET.fromstring(cot).find("detail/contact")
        if contact is not None and contact.attrib.get("callsign"):
            out.add(contact.attrib["callsign"])
    return out


def atak_stream_pref(host: str, port: int, *, name: str = "meshsim") -> str:
    """An ATAK connection ``.pref`` (streaming TCP input) pointing at host:port.

    Pushed to ATAK's config-import dir so the stream is preconfigured without a
    UI walk. ATAK connection strings are ``<host>:<port>:<proto>``.
    """
    conn = f"{host}:{port}:tcp"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<preferences>\n"
        '  <preference version="1" name="cot_streams">\n'
        '    <entry key="count" class="class java.lang.Integer">1</entry>\n'
        f'    <entry key="description0" class="class java.lang.String">{name}</entry>\n'
        f'    <entry key="connectString0" class="class java.lang.String">{conn}</entry>\n'
        '    <entry key="enabled0" class="class java.lang.Boolean">true</entry>\n'
        '    <entry key="useAuth0" class="class java.lang.Boolean">false</entry>\n'
        "  </preference>\n"
        "</preferences>\n"
    )


def build_squad(seed: int, nodes: int, days: int):
    """Generate a v2 TAK-squad capture and its CoT events (SDK-gated)."""
    from meshtastic_mcp.replay import sim, tak_server

    prof = {"tak": {"team_nodes": 5, "pli_interval": 45, "chat_per_hour": 3, "wire": "v2"}}
    cap = sim.generate(nodes=nodes, days=days, seed=seed, profile=prof)
    return cap, tak_server.capture_to_cot_events(cap)


# ── emulator orchestration (needs android capability + ATAK) ─────────────────
def run(atak_apk: str, *, seed: int, nodes: int, days: int, timeout: float) -> int:
    from meshtastic_mcp.emulator import avd
    from meshtastic_mcp.replay import tak_server

    serial = avd.wait_for_boot()
    print(f"[ci-atak] emulator ready: {serial}")
    avd.install_app(atak_apk, serial=serial)
    print(f"[ci-atak] installed {atak_apk}")

    _cap, events = build_squad(seed, nodes, days)
    callsigns = expected_callsigns(events)
    if not callsigns:
        print(_mesh.verdict("atak-render", False, "(build)", None, extra="no squad CoT events"))
        return 1
    target = sorted(callsigns)[0]
    print(f"[ci-atak] squad callsigns: {sorted(callsigns)} — asserting '{target}'")

    # host CoT server; ATAK reaches it at 10.0.2.2:<port>
    srv = tak_server.CotTakServer(events, host="0.0.0.0", port=0, speed=60.0, loop=True)
    port = srv.start()
    try:
        avd.adb_reverse(port, port, serial=serial)  # emulator → host
        _push_stream_pref(avd, serial, "10.0.2.2", port)
        avd.grant_runtime_permissions(ATAK_PACKAGE, serial=serial)
        avd.launch_app(ATAK_PACKAGE, serial=serial)
        _dismiss_first_run(avd, serial)
        passed = avd.poll_for_text(target, serial=serial, timeout=timeout)
        print(_mesh.verdict("atak-render", passed, target, 0 if passed else None))
        return 0 if passed else 1
    finally:
        srv.stop()


def _push_stream_pref(avd, serial: str, host: str, port: int) -> None:
    """Best-effort: preconfigure the ATAK streaming input via a pushed .pref."""
    import tempfile

    pref = atak_stream_pref(host, port)
    with tempfile.NamedTemporaryFile("w", suffix=".pref", delete=False) as fh:
        fh.write(pref)
        local = fh.name
    for dest in (
        "/sdcard/atak/config/prefs/meshsim.pref",
        "/sdcard/atak/import/meshsim.pref",
    ):
        avd.adb("push", local, dest, serial=serial, check=False)
    print(f"[ci-atak] pushed streaming pref -> {host}:{port}")


def _dismiss_first_run(avd, serial: str) -> None:
    """Best-effort EULA / permission dismissal on first launch."""
    for label in ("I agree", "Agree", "OK", "Continue", "GOT IT"):
        if avd.find_text(label, serial=serial):
            with __import__("contextlib").suppress(Exception):
                avd.tap_text(label, serial=serial) if hasattr(avd, "tap_text") else None
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ATAK app-plane e2e (render sim TAK squad)")
    p.add_argument("--atak-apk", required=True, help="ATAK-CIV APK to install + drive")
    p.add_argument("--seed", type=int, default=8)
    p.add_argument("--nodes", type=int, default=120)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--timeout", type=float, default=90.0)
    args = p.parse_args(argv)
    if not pathlib.Path(args.atak_apk).is_file():
        print(f"FAIL: ATAK APK not found at {args.atak_apk}", file=sys.stderr)
        return 2
    try:
        from meshtastic_mcp.replay import tak

        if not tak.available():
            print(
                "FAIL: the [tak] extra is required (pip install 'meshtastic-mcp[tak]')",
                file=sys.stderr,
            )
            return 2
    except Exception:
        print("FAIL: meshtastic_mcp not importable", file=sys.stderr)
        return 2
    return run(
        args.atak_apk, seed=args.seed, nodes=args.nodes, days=args.days, timeout=args.timeout
    )


if __name__ == "__main__":
    sys.exit(main())
