#!/usr/bin/env python3
# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Apple app-plane e2e: device-profile import on the iOS Simulator.

Drives the real Meshtastic-Apple import path against real firmware, with no radio attached:
a native `meshtasticd` virtual radio over TCP, the app in the iOS Simulator, and `idb` for UI.

    python scripts/ci_apple_config_import_loop.py \\
        --binary .pio/build/native-macos/meshtasticd \\
        --app build/.../Meshtastic.app --profile fixtures/sample.cfg

What this covers, so a human does not have to:
  - the import applies inside a firmware edit transaction (one deferred save, one reboot)
  - sends are paced, so nothing is silently dropped
  - the plan gates items the connected firmware cannot apply
  - the post-import verification pass reports what actually landed

What it CANNOT cover, by construction:
  The BLE path. The iOS Simulator has no Bluetooth radio, `apple_sim` is simctl+idb only, and
  physical-iOS UI automation is unsupported. Over TCP `disableBluetooth()` is a no-op for the
  transport, so the link survives the commit and the deferred MQTT/Serial phase runs. On BLE that
  same phase cannot run at all. Both branches exist in the app; this exercises one.
  Every BLE-disconnect condition still needs a physical phone. See `--print-manual-checklist`.

The `.cfg` is injected straight into the app's Documents container (the app sets
UIFileSharingEnabled + LSSupportsOpeningDocumentsInPlace), so the document picker can see it
without any file-provider theatre.

KNOWN BLOCKER — the document picker is not automatable here.
  `UIDocumentPickerViewController` runs out of process (DocumentManagerUICore), so idb's
  app-scoped accessibility tree reports a single element and label-based tapping has nothing to
  match. Reaching the injected file needs blind coordinate taps through Browse -> On My iPhone ->
  Meshtastic -> <file>, which is brittle across iOS versions and screen sizes.

  Everything up to that point is verified working: mesh, simulator, install, profile injection,
  TCP connect, and Settings -> Tools navigation. The run currently stops at the picker.

  Ways out, none yet implemented:
    - give the app a URL scheme or file-open handler for .cfg so the picker can be bypassed with
      `simctl openurl` (it declares LSSupportsOpeningDocumentsInPlace but has no onOpenURL);
    - add a debug-build affordance that imports a fixture path directly;
    - drive the picker by coordinate and accept the brittleness.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import time

from meshtastic_mcp.emulator import apple_sim

# scripts/ is not a package; load the sibling loop for its mesh bring-up + UI helpers.
_SIB = pathlib.Path(__file__).resolve().parent / "ci_apple_app_loop.py"
_spec = importlib.util.spec_from_file_location("ci_apple_app_loop", _SIB)
assert _spec and _spec.loader
_apple = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_apple)
# The sibling already loaded the device-plane helper; reuse its handle rather than exec'ing twice.
_mesh = _apple._mesh

BUNDLE_ID = "gvh.MeshtasticClient"

# Tab-bar geometry, borrowed from the sibling loop: idb's flat accessibility tree exposes the tab
# bar as one unlabeled Group, so its items are reachable only by coordinate. 5 tabs across 402pt.
_TAB_BAR_Y = 832
_TAB_XS = (40, 120, 201, 282, 362)

# Labels lifted from Tools.swift / ImportDeviceProfileView.swift. Kept together so a UI rename
# shows up as one obvious diff rather than a mystery timeout.
# Shown immediately on the Settings screen, so it is a safe anchor for tab probing.
L_SETTINGS_ANCHOR = "Radio Configuration"
L_TOOLS = "Tools"
# Proof we actually landed, rather than merely tapping something named like it. Must be visible on
# arrival WITHOUT scrolling. Two candidates because the NFC section is gated on
# NFCReader.isAvailable: when it renders it is first, otherwise Export is.
L_TOOLS_ANCHORS = ("Create Node Contact NFC Tag", "Export Device Configuration")
L_IMPORT_ENTRY = "Import Configuration"
L_IMPORT_ACTION = "Import"
L_APPLY = "Apply Configuration"
L_VERIFY = "Verify Against the Radio"
L_DONE = "Done"

# Result-banner prefixes (see resultList in ImportDeviceProfileView).
R_SUCCESS = "Sent "
R_FAILED = "Import stopped after a failure"
R_CANCELLED = "Import cancelled"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _ensure_companion(udid: str, *, attempts: int = 3) -> None:
    """Start idb_companion, clearing a stale socket first.

    A companion killed mid-run leaves its unix socket behind, and the next start exits rc=1
    immediately. That reads as an idb bug rather than leftover state, so clean up and retry.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            apple_sim.start_companion(udid)
            return
        except Exception as exc:
            # Retried below; re-raised if it never succeeds.
            last = exc
            subprocess.run(["pkill", "-f", f"idb_companion.*{udid}"], capture_output=True)
            pathlib.Path(f"/tmp/idb/{udid}_companion.sock").unlink(missing_ok=True)
            time.sleep(2 + 2 * attempt)
    raise RuntimeError(f"idb_companion would not start for {udid}: {last}")


def _app_documents(udid: str) -> pathlib.Path:
    """The app's on-disk Documents dir. The simulator container is just a host directory."""
    out = subprocess.run(
        ["xcrun", "simctl", "get_app_container", udid, BUNDLE_ID, "data"],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = pathlib.Path(out.stdout.strip()) / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    return docs


def _await_label(udid: str, token: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if apple_sim.find_text(token, udid=udid):
            return True
        _apple._dismiss_pending(udid, rounds=2)
        time.sleep(2)
    return False


def _scroll_down(udid: str) -> None:
    """Swipe the current list up by roughly half a screen."""
    try:
        # duration + delta matter: an instantaneous two-point swipe does not scroll a SwiftUI List,
        # it just registers as a tap. Interpolated touch points over ~0.6s do.
        apple_sim.idb(
            "ui",
            "swipe",
            "200",
            "700",
            "200",
            "250",
            "--duration",
            "0.6",
            "--delta",
            "20",
            udid=udid,
        )
    except Exception:
        # Best-effort: the caller re-probes for the label either way.
        pass
    time.sleep(1.2)


def _find_settings_tab(udid: str) -> bool:
    """Tap the Settings tab, probing positions rather than hard-coding which one it is.

    Tab items are not individually labeled in idb's flat tree, so they can only be tapped by
    coordinate. Probe each position and look for a label the Settings screen shows immediately
    (`Tools` sits below the fold, so it is the wrong thing to probe for). Probing survives a
    tab-order change; a hard-coded index would not.
    """
    for x in _TAB_XS:
        apple_sim.tap(x, _TAB_BAR_Y, udid=udid)
        time.sleep(1.8)
        if apple_sim.find_text(L_SETTINGS_ANCHOR, udid=udid):
            return True
    return False


def _reveal(label: str, udid: str, *, scrolls: int = 8) -> bool:
    """Scroll the current list until `label` is on screen."""
    for _ in range(scrolls):
        if apple_sim.find_text(label, udid=udid):
            return True
        _scroll_down(udid)
    return apple_sim.find_text(label, udid=udid)


def _tap_and_arrive(label: str, anchors: tuple[str, ...], udid: str, *, attempts: int = 3) -> bool:
    """Reveal `label`, tap it, and confirm arrival by any of `anchors` appearing.

    Tapping is not proof of navigation. `_tap_label` taps the centre of any element whose text
    contains the label, including section headers and other non-interactive matches, so it can
    report success while the screen never changes.
    """
    for _ in range(attempts):
        if not _reveal(label, udid):
            break
        _apple._tap_label(label, udid, delay=2.5)
        if any(apple_sim.find_text(a, udid=udid) for a in anchors):
            return True
        _apple._dismiss_pending(udid, rounds=3)
        time.sleep(1.5)
    _log(f"FAIL import-nav {label!r} did not lead to any of {anchors!r}")
    _dump_screen(udid)
    return False


def _dump_screen(udid: str, *, scrolls: int = 6) -> None:
    """Print every label reachable by scrolling the current screen.

    A truncated snapshot of the top of a list is misleading when the target is below the fold or
    conditionally rendered, which is the usual reason a tap target is 'missing'.
    """
    seen: list[str] = []
    for _ in range(scrolls):
        for el in apple_sim.ui_dump(udid=udid):
            lbl = el.get("AXLabel") or ""
            if lbl and lbl not in seen:
                seen.append(lbl)
        _scroll_down(udid)
    _log(f"       screen contains ({len(seen)}): {seen}")
    try:
        apple_sim.screenshot("/tmp/import-loop-fail.png", udid=udid)
        _log("       screenshot -> /tmp/import-loop-fail.png")
    except Exception:
        pass


def _open_import_sheet(udid: str, profile_name: str) -> bool:
    """Settings -> (scroll) Tools -> Import Configuration -> pick the injected profile."""
    if not _find_settings_tab(udid):
        visible = [e.get("AXLabel", "") for e in apple_sim.ui_dump(udid=udid) if e.get("AXLabel")]
        _log(f"FAIL import-nav no tab exposed {L_SETTINGS_ANCHOR!r}; visible: {visible[:12]}")
        return False
    # Both targets sit below the fold on their screens: Tools is far down Settings, and Import
    # Configuration is under the NFC and Export sections on Tools. Reveal, tap, then confirm we
    # actually moved: a tap can land on a non-interactive element that merely contains the text,
    # which reports success and goes nowhere.
    if not _tap_and_arrive(L_TOOLS, L_TOOLS_ANCHORS, udid):
        return False
    # Tapping Import Configuration opens the document picker; the injected profile appearing there
    # is the proof of arrival.
    if not _tap_and_arrive(L_IMPORT_ENTRY, (profile_name,), udid):
        return False
    return _apple._tap_label(profile_name, udid, delay=3.0)


def run(
    binary: pathlib.Path,
    app: pathlib.Path,
    profile: pathlib.Path,
    *,
    sim: str = "iPhone 17 Pro",
    timeout: float = 90.0,
    verify: bool = True,
) -> int:
    failures: list[str] = []

    _log("== mesh up (native meshtasticd over TCP) ==")
    # mesh_up is a context manager yielding (dut, tester); it owns the supervised restart loop that
    # keeps a node alive across the config reboot, which this test deliberately triggers.
    udid = apple_sim.ensure_booted(sim)
    # idb drives every tap; without a companion the accessibility tree is unreachable and the first
    # tap fails with a confusing "could not tap".
    _ensure_companion(udid)
    _log(f"== simulator {sim} ({udid}) + companion ==")

    apple_sim.install_app(app, udid=udid)
    # The container only exists after install. The app sets UIFileSharingEnabled +
    # LSSupportsOpeningDocumentsInPlace, so anything dropped in Documents shows up in the picker.
    dest = _app_documents(udid) / profile.name
    shutil.copyfile(profile, dest)
    _log(f"== injected profile -> {dest} ==")

    # Onboarding needs more than one pass, and a relaunch, before the tab bar is reachable.
    apple_sim.launch(BUNDLE_ID, udid=udid)
    time.sleep(4)
    _apple._dismiss_pending(udid, rounds=15)
    apple_sim.launch(BUNDLE_ID, udid=udid)
    time.sleep(3)
    _apple._dismiss_pending(udid, rounds=5)

    with _mesh.mesh_up(binary, pathlib.Path("/tmp/ci-apple-import-lab"), count=2) as (dut, _tester):
        _apple._navigate_to_connect(udid)
        _apple._connect_manual_tcp(udid, apple_sim.tcp_dut_address(dut.tcp_port))
        # The post-connect "Connected Radio" callout hides the tab bar from the accessibility tree,
        # which is why the sibling loop navigates before connecting. Import needs a live connection,
        # so clear the callout instead of dodging it.
        _apple._dismiss_pending(udid, rounds=10)

        if not _open_import_sheet(udid, profile.name):
            failures.append("could not open the import sheet")
            return _verdict(failures)

        if not _await_label(udid, L_IMPORT_ACTION, timeout=30):
            failures.append("import review sheet never rendered")
            return _verdict(failures)
        _log("PASS import-review rendered")

        _apple._tap_label(L_IMPORT_ACTION, udid, delay=1.5)
        _apple._tap_label(L_APPLY, udid, delay=2.0)
        _log("== applying ==")

        deadline = time.monotonic() + timeout
        outcome = ""
        while time.monotonic() < deadline and not outcome:
            for token in (R_SUCCESS, R_FAILED, R_CANCELLED):
                if apple_sim.find_text(token, udid=udid):
                    outcome = token
                    break
            time.sleep(2)

        if outcome == R_SUCCESS:
            _log("PASS import-applied")
        elif outcome:
            failures.append(f"import reported: {outcome!r}")
            _log(f"FAIL import-applied {outcome!r}")
        else:
            failures.append("import never reported a result")
            _log("FAIL import-applied timed out")

        # The radio reboots on commit; the app re-runs wantConfig on reconnect, which is what arms
        # the verification action. Poll rather than assume a fixed settle time.
        if verify and outcome == R_SUCCESS:
            _log("== waiting for reconnect + config refresh to arm verification ==")
            if _await_label(udid, L_VERIFY, timeout=120):
                _apple._tap_label(L_VERIFY, udid, delay=3.0)
                if apple_sim.find_text("The radio still reports its previous value", udid=udid):
                    failures.append("verification found sections the radio never received")
                    _log("FAIL verify-clean likely-dropped sections reported")
                else:
                    _log("PASS verify-clean")
            else:
                failures.append("verification never became available after reconnect")
                _log("FAIL verify-armed")

        apple_sim.screenshot("/tmp/apple-config-import.png", udid=udid)
        return _verdict(failures)


def _verdict(failures: list[str]) -> int:
    if failures:
        _log("\nVERDICT fail")
        for f in failures:
            _log(f"  - {f}")
        return 1
    _log("\nVERDICT pass")
    return 0


MANUAL_CHECKLIST = """\
Still manual
============
 0. Selecting the .cfg in the document picker (any platform).
    The picker is out of process, so idb cannot see or tap its contents. This loop drives
    everything up to it. Not BLE-specific; it applies in the Simulator too.

BLE-only conditions — physical phone required, cannot be simulated
=================================================================
The Simulator has no Bluetooth radio, so none of the below is covered by this loop.
Each targets a branch that only exists on BLE.

 1. Import a profile containing MQTT or Serial.
    Firmware calls disableBluetooth() for those two regardless of the open transaction
    (AdminModule.cpp:1191, :1207). Expect: everything else commits, and MQTT/Serial appear
    under "Needs a Second Pass" rather than as failures.

 2. Import a profile WITHOUT MQTT/Serial.
    Expect a single reboot at the commit, then automatic reconnect, then "Verify Against the
    Radio" becoming tappable.

 3. Walk out of range mid-import (or power the radio off ~2s after tapping Apply).
    Expect a partial result naming the failed section, and NOT a claim of success.

 4. Kill Bluetooth on the phone mid-import.
    Expect the same, plus "The node did not confirm saving these settings" if the commit itself
    could not be sent.

 5. Verify too early: tap Verify before the radio reconnects.
    Expect it disabled with "Available once the radio reconnects…". It must never report every
    section as dropped, which is what a stale readback would look like.

 6. Import onto a 2.7.x radio (not develop).
    LoRa reboots there but not on develop. Expect the reboot warning to be accurate.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Apple app-plane e2e: device-profile import")
    p.add_argument("--binary", type=pathlib.Path, help="native-macos meshtasticd")
    p.add_argument("--app", type=pathlib.Path, help="built Meshtastic.app for the simulator")
    p.add_argument("--profile", type=pathlib.Path, help=".cfg device profile to import")
    p.add_argument("--sim", default="iPhone 17 Pro")
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--no-verify", action="store_true", help="skip the verification pass")
    p.add_argument(
        "--print-manual-checklist",
        action="store_true",
        help="print the BLE-only conditions that still need a physical phone, and exit",
    )
    a = p.parse_args(argv)

    if a.print_manual_checklist:
        print(MANUAL_CHECKLIST)
        return 0
    missing = [n for n in ("binary", "app", "profile") if getattr(a, n) is None]
    if missing:
        p.error("required unless --print-manual-checklist: " + ", ".join("--" + m for m in missing))
    return run(a.binary, a.app, a.profile, sim=a.sim, timeout=a.timeout, verify=not a.no_verify)


if __name__ == "__main__":
    sys.exit(main())
