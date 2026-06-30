#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Apple app-plane e2e for CI: device->app inbound loop on the iOS Simulator.

The Apple counterpart of `ci_android_app_loop.py`. Brings up the virtual mesh (reusing
`ci_device_mesh_e2e.mesh_up`), boots an iOS Simulator, installs + launches the Meshtastic-Apple
app, drives onboarding + the Manual-TCP connect to the DUT (`127.0.0.1:<port>`, shared host
stack — no `10.0.2.2` alias), then has the tester broadcast a unique token and asserts the app
**renders** it via the accessibility tree (`idb`).

    python scripts/ci_apple_app_loop.py \\
        --binary .pio/build/native-macos/meshtasticd \\
        --app build/.../Meshtastic.app --bundle-id gvh.MeshtasticClient

Design notes (validated live 2026-06-25; see references/simulator-apple.md):
- Build with ad-hoc signing that KEEPS entitlements (no CODE_SIGNING_ALLOWED=NO).
- System permission alerts ARE visible in the idb accessibility tree via AXLabel — no
  coordinate hacks needed; label-based tapping works for both app buttons and system dialogs.
- Permission dialogs can appear at any point (initial launch, mesh startup, connect flow).
  `_dismiss_pending()` is called opportunistically throughout.
- The mesh (mesh_up) starts BEFORE we navigate to the Connect tab, so all the ~32s of
  node boot/configure/warmup overlaps with app setup, reducing total wall time.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import time

from meshtastic_mcp.emulator import apple_sim

# Reuse the mesh bring-up + verdict from the device-plane helper (scripts/ is not a package).
_SIB = pathlib.Path(__file__).resolve().parent / "ci_device_mesh_e2e.py"
_spec = importlib.util.spec_from_file_location("ci_device_mesh_e2e", _SIB)
assert _spec and _spec.loader
_mesh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mesh)

# System/app buttons to grant / advance when they appear. Order matters: prefer the
# more-permissive choice so location/notifications/Siri all work during testing.
_GRANT = [
    "Allow While Using App",
    "Allow Once",
    "Allow",
    "Change to Always Allow",  # location escalation ("always allow" = most permissive)
    "Keep Only While Using",  # location escalation fallback
    "Get started",
    "Continue",
]


_INTERACTIVE = {"Button", "Cell", "MenuItem", "PopUpButton", "Link", "Switch"}


def _element_center(label: str, udid: str) -> tuple[int, int] | None:
    """Find an element whose text contains `label`; prefer interactive types over static text.

    Without the preference, labels like "Allow" match the *StaticText* of a permission
    prompt body (which starts with "Allow Meshtastic to...") before they match the *Button*
    of the same name — tapping static text does nothing.
    """
    candidates: list[tuple[int, int, int]] = []  # (priority, cx, cy) — lower = better
    for el in apple_sim.ui_dump(udid=udid):
        text = " ".join(
            str(el.get(k, "")) for k in ("AXLabel", "AXValue", "label", "title", "value")
        )
        if label.lower() not in text.lower():
            continue
        frame = el.get("frame") or el.get("AXFrame") or {}
        try:
            cx = int(float(frame["x"]) + float(frame["width"]) / 2)
            cy = int(float(frame["y"]) + float(frame["height"]) / 2)
        except (KeyError, TypeError, ValueError):
            continue
        priority = 0 if el.get("type") in _INTERACTIVE else 1
        candidates.append((priority, cx, cy))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1], candidates[0][2]


def _tap_label(label: str, udid: str, *, delay: float = 1.5) -> bool:
    center = _element_center(label, udid)
    if center is None:
        return False
    apple_sim.tap(*center, udid=udid)
    time.sleep(delay)
    return True


def _dismiss_pending(udid: str, *, rounds: int = 8) -> None:
    """Dismiss all visible onboarding / permission prompts (best-effort, bounded)."""
    for _ in range(rounds):
        tapped = False
        for lbl in _GRANT:
            if _tap_label(lbl, udid, delay=1.2):
                tapped = True
                break
        if not tapped:
            break


def _navigate_to_connect(udid: str) -> None:
    """Tap the Connect tab. Uses coordinates (tab items not labeled in the a11y tree)."""
    apple_sim.tap(_TAB_CONNECT_X, _TAB_BAR_Y, udid=udid)
    time.sleep(1.5)


def _connect_manual_tcp(udid: str, addr: str) -> None:
    """Connect → + Manual → TCP popup → type addr → OK, with dialog sweeps at each step."""
    _dismiss_pending(udid)  # clear anything that appeared during mesh startup
    _navigate_to_connect(udid)

    # Try tapping an existing saved connection row first (fast path on re-runs).
    if _tap_label(addr, udid):
        return

    # Full flow: + Manual → TCP popup → type addr → OK.
    if not _tap_label("Manual", udid):
        _tap_label("+", udid)
    time.sleep(0.5)
    _tap_label("TCP", udid)
    time.sleep(0.5)
    apple_sim.type_text(addr, udid=udid)
    time.sleep(0.3)
    _tap_label("OK", udid)


# Tab bar item coordinates for iPhone 17 Pro (402×874 pt, 5 tabs of width≈80pt each).
# Tab bar Group is at y=791, height=83 → center y≈832.
_TAB_BAR_Y = 832
_TAB_MESSAGES_X = 40  # first of 5 tabs
_TAB_CONNECT_X = 362  # last of 5 tabs


def _navigate_to_primary_channel(udid: str) -> bool:
    """Messages tab (coord) → Channels (label) → Primary Channel (label).

    The tab bar Group is not individually labeled in idb's flat accessibility tree —
    tab items are children inaccessible by label.  Use coordinates for the tab tap.
    Must be called BEFORE connecting (no post-connect callout yet).
    """
    # Messages tab via coordinate (not accessible by label).
    apple_sim.tap(_TAB_MESSAGES_X, _TAB_BAR_Y, udid=udid)
    time.sleep(1.5)
    # Channels and Primary Channel ARE accessible by label inside the Messages view.
    ok2 = _tap_label("Channels", udid)
    time.sleep(1)
    ok3 = _tap_label("Primary Channel", udid)
    time.sleep(1)
    print(f"[ci-apple] nav to channel: Messages=coord Channels={ok2} PrimaryChannel={ok3}")
    return ok2 and ok3


def run(
    binary: pathlib.Path,
    app: str,
    bundle_id: str,
    sim: str,
    timeout: float,
) -> int:
    # 1. Boot sim + companion (idb_companion lifecycle is now managed inside apple_sim).
    udid = apple_sim.ensure_booted(sim)
    apple_sim.start_companion(udid)  # idempotent: disconnects stale + reconnects
    print(f"[ci-apple] sim booted + companion: {udid}")

    # 2. Install + launch the app.
    apple_sim.install_app(app, udid=udid)
    apple_sim.launch(bundle_id, udid=udid)
    time.sleep(4)

    # 3. Dismiss onboarding + navigate to Primary Channel BEFORE starting the mesh.
    #    The 'Connected Radio' callout that appears after first connect hides the tab bar
    #    from the accessibility tree, so we must navigate while it is still clear.
    _dismiss_pending(udid, rounds=15)
    apple_sim.launch(bundle_id, udid=udid)
    time.sleep(3)
    _dismiss_pending(udid, rounds=5)
    nav_ok = _navigate_to_primary_channel(udid)
    if not nav_ok:
        print(
            "[ci-apple] WARNING: navigation incomplete — visible:",
            [e.get("AXLabel", "") for e in apple_sim.ui_dump(udid=udid) if e.get("AXLabel")][:8],
        )

    # 4. Start the mesh (takes ~32s). Now mesh is up when we connect.
    try:
        with _mesh.mesh_up(binary, pathlib.Path("/tmp/ci-apple-lab"), count=2) as (dut, tester):
            # 5. Connect the app to the DUT over TCP.  Poll while dismissing dialogs that
            #    may appear (location-always escalation, etc.).
            addr = apple_sim.tcp_dut_address(dut.tcp_port)
            print(f"[ci-apple] connecting app to {addr}")
            _connect_manual_tcp(udid, addr)

            # Poll for connection confirmation while dismissing any dialogs that
            # appear mid-flow (e.g. location-always-on escalation after first connect).
            deadline = time.time() + 30
            connected = False
            while time.time() < deadline:
                _dismiss_pending(udid, rounds=1)
                if apple_sim.find_text("Subscribed", udid=udid) or apple_sim.find_text(
                    "Connected Radio", udid=udid
                ):
                    connected = True
                    break
                time.sleep(1)
            if not connected:
                print(
                    _mesh.verdict("inbound", False, "(connect)", None, extra="app did not connect")
                )
                return 1

            # 7. Navigate back to Primary Channel. The 'Connected Radio' callout may be
            # showing (blocks a11y labels), but coordinate taps reach the tab bar regardless.
            apple_sim.tap(_TAB_MESSAGES_X, _TAB_BAR_Y, udid=udid)  # Messages tab coord
            time.sleep(1.5)
            _tap_label("Channels", udid)
            time.sleep(1)
            _tap_label("Primary Channel", udid)
            time.sleep(1)

            # 8. Tester broadcasts; poll on the Primary Channel screen.
            token = f"CI-APPLE-{int(time.time())}"
            _mesh._broadcast(tester.tcp_port, token)
            passed = apple_sim.poll_for_text(token, udid=udid, timeout=timeout)
            if not passed:  # one retry
                _mesh._broadcast(tester.tcp_port, token)
                passed = apple_sim.poll_for_text(token, udid=udid, timeout=timeout)
            print(_mesh.verdict("inbound", passed, token, 0 if passed else None))
            return 0 if passed else 1
    finally:
        apple_sim.stop_companion(udid)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CI apple app-plane e2e (device->app inbound)")
    p.add_argument("--binary", type=pathlib.Path, required=True, help="native-macos meshtasticd")
    p.add_argument(
        "--app", required=True, help="Meshtastic.app bundle (ad-hoc signed, keeps entitlements)"
    )
    p.add_argument("--bundle-id", default="gvh.MeshtasticClient")
    p.add_argument("--sim", default="iPhone 17 Pro", help="simulator device name to boot")
    p.add_argument("--timeout", type=float, default=45.0)
    args = p.parse_args(argv)
    if not args.binary.is_file():
        print(f"FAIL: meshtasticd binary not found at {args.binary}", file=sys.stderr)
        return 2
    if not pathlib.Path(args.app).exists():
        print(f"FAIL: app bundle not found at {args.app}", file=sys.stderr)
        return 2
    return run(args.binary, args.app, args.bundle_id, args.sim, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
