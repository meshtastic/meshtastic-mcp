#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Android app-plane e2e for CI: device->app inbound loop on a running Android emulator.

The full-stack counterpart of `ci_device_mesh_e2e.py`. Assumes an emulator is **already
booted** (e.g. by `reactivecircus/android-emulator-runner`, whose `script:` runs with the AVD
up). It brings up the virtual mesh (reusing `ci_device_mesh_e2e.mesh_up`), installs the
Meshtastic-Android APK, connects the app to the DUT node over TCP (`10.0.2.2:<port>`), then has
the tester node broadcast a unique token and asserts the app **renders** it.

    python scripts/ci_android_app_loop.py \
        --binary .pio/build/native/meshtasticd --apk app-debug.apk

Emits `LOOP inbound PASS|FAIL …` and exits non-zero on FAIL, matching the device-plane leg.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import time

from meshtastic_mcp.emulator import avd

# Reuse the mesh bring-up + verdict from the sibling helper (scripts/ is not a package).
_SIB = pathlib.Path(__file__).resolve().parent / "ci_device_mesh_e2e.py"
_spec = importlib.util.spec_from_file_location("ci_device_mesh_e2e", _SIB)
assert _spec and _spec.loader
_mesh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mesh)


def run(binary: pathlib.Path, apk: str, timeout: float) -> int:
    serial = avd.wait_for_boot()
    print(f"[ci-emu] emulator ready: {serial}")
    avd.install_app(apk, serial=serial)
    print(f"[ci-emu] installed {apk}")

    with _mesh.mesh_up(binary, pathlib.Path("/tmp/ci-emu-lab"), count=2) as (dut, tester):
        dut_addr = avd.tcp_dut_address(dut.tcp_port)
        print(f"[ci-emu] connecting app to {dut_addr}")
        avd.connect_app_to_tcp(dut_addr, serial=serial)
        # Confirm the radio is connected before asserting message delivery.
        if not avd.poll_for_text("Disconnect", serial=serial, timeout=30):
            print(_mesh.verdict("inbound", False, "(connect)", None, extra="app did not connect"))
            return 1

        token = f"CI-EMU-{int(time.time())}"
        avd_broadcast_from = tester.tcp_port
        _mesh._broadcast(avd_broadcast_from, token)  # tester broadcasts over the mesh
        passed = avd.poll_for_text(token, serial=serial, timeout=timeout)
        if not passed:  # one retry — mesh delivery is best-effort
            _mesh._broadcast(avd_broadcast_from, token)
            passed = avd.poll_for_text(token, serial=serial, timeout=timeout)
        print(_mesh.verdict("inbound", passed, token, 0 if passed else None))
        return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CI emulator app-plane e2e (device->app inbound)")
    p.add_argument("--binary", type=pathlib.Path, required=True, help="native meshtasticd path")
    p.add_argument("--apk", required=True, help="Meshtastic-Android APK to install + drive")
    p.add_argument("--timeout", type=float, default=45.0)
    args = p.parse_args(argv)
    if not args.binary.is_file():
        print(f"FAIL: meshtasticd binary not found at {args.binary}", file=sys.stderr)
        return 2
    if not pathlib.Path(args.apk).is_file():
        print(f"FAIL: APK not found at {args.apk}", file=sys.stderr)
        return 2
    return run(args.binary, args.apk, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
