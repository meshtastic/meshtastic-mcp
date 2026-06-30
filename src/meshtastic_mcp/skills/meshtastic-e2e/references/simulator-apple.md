# Apple lab: hardware-free closed loop (iOS Simulator / macOS app + native nodes)

The Apple counterpart of `emulator-lab.md`. The **device plane is identical and reused
verbatim** — `meshtastic_mcp.emulator.native_node` UDP-multicast mesh + the recorder. Only the
app plane changes (Meshtastic-Apple instead of Meshtastic-Android).

```
host (macOS)
  meshtasticd #1 (DUT)  ──┐
  meshtasticd #2 (tester)  ├─ UDP multicast mesh (224.0.0.69:4403)
       ▲                   │        ▲
  TCP :4403           TCP :4404
       │                   │
  iOS Simulator        MCP server (tcp://127.0.0.1:4404)
  app → 127.0.0.1:4403
```

## Why this is simpler than Android

- **The iOS Simulator shares the host network stack** → the app connects to
  `127.0.0.1:<port>` directly. No `10.0.2.2` alias.
- A **native macOS build** needs no simulator at all — it connects to localhost directly,
  the absolute simplest loop.
- BLE is still not simulatable — TCP-only path, same as Android.

## Building blocks (this package)

- **Device plane:** `meshtastic_mcp.emulator.native_node` (unchanged — see `topology.md`).
- **App plane:** `meshtastic_mcp.emulator.apple_sim` — wraps `xcrun simctl` + `idb`:
  - `list_simulators()` / `ensure_booted("iPhone")` / `shutdown()` — lifecycle (simctl)
  - `install_app(app_path)` / `launch(bundle_id)` / `is_app_installed()` — deploy (simctl)
  - `ui_dump()` — accessibility tree via `idb ui describe-all` (the `android layout` analog)
  - `tap` / `type_text` — input via `idb ui tap` / `idb ui text`
  - `screenshot()` — `xcrun simctl io booted screenshot`
  - `find_text` / `poll_for_text(token, timeout=…)` — bounded app-plane oracle
  - `tcp_dut_address(port)` → `127.0.0.1:<port>`

## Prerequisites

```bash
xcrun --version                                   # Xcode / command-line tools (the `apple` capability)
# idb_companion: the cli is in the facebook tap (NOT the `companion` cask, which is unrelated).
brew tap facebook/fb && brew trust facebook/fb && brew install facebook/fb/idb-companion
# fb-idb (the client) breaks on Python 3.14 (asyncio.get_event_loop removed) — pin <=3.12:
brew install python@3.12 && pipx install --python /opt/homebrew/bin/python3.12 fb-idb
```

### iOS Simulator build gotchas (full true-sim loop validated live 2026-06-25)

The true iOS-Simulator loop is **proven end-to-end** (inbound bubble rendered ~2 s). Three
gotchas, each with the live-confirmed fix:

**1. watchOS SDK + a *usable* runtime.** The `Meshtastic` scheme embeds the Apple Watch app, so
the iOS-Simulator build needs watchOS, not just iOS.
   - `xcodebuild -downloadPlatform watchOS` installs the SDK (~4 GB). Without it: *"watchOS X
     must be installed in order to run the scheme."*
   - If `xcrun simctl list runtimes` doesn't show watchOS even though `xcrun simctl runtime list`
     says *Ready*, the **root cause is duplicate disk images**, not a missing reboot. Check
     `xcrun simctl runtime list` for entries marked `Unusable - … Duplicate of <UUID>`; a stale
     "Ready" copy can also be unmounted (its `Mount Path` dir is absent + `simctl runtime verify`
     fails "cannot find code object on disk"). **Fix: delete every watchOS image
     (`xcrun simctl runtime delete <UUID>`), then re-download once** — it then registers as a
     usable runtime. A reboot is *not* required (the earlier "reboot fixes it" note was a
     red herring — the duplicates were the real blocker).

**2. Build with ad-hoc signing that KEEPS entitlements — not `CODE_SIGNING_ALLOWED=NO`.** The
app's `AppDelegate.didFinishLaunchingWithOptions` calls `INPreferences.requestSiriAuthorization`,
which requires `com.apple.developer.siri`. `CODE_SIGNING_ALLOWED=NO` strips entitlements, so the
app **crashes on launch**: *"Use of the class <INPreferences> … requires the entitlement
com.apple.developer.siri."* The simulator honors restricted entitlements ad-hoc (no dev account
needed), so build with:
   ```bash
   xcodebuild -workspace Meshtastic.xcworkspace -scheme Meshtastic \
     -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
     -derivedDataPath build \
     CODE_SIGN_IDENTITY="-" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=YES \
     AD_HOC_CODE_SIGNING_ALLOWED=YES build
   APP=build/Build/Products/Debug-iphonesimulator/Meshtastic.app
   ```

**3. Diagnose launch crashes with `simctl launch --console-pty`** — `simctl launch` returns a PID
even when the app immediately throws; `--console-pty` surfaces the exception + stack trace.

The macOS (Catalyst) target (next section) is still the *fastest* path (no watchOS, no idb), but
the iOS Simulator path above is now fully working when you need to test real iOS UI.

## Recipe (iOS Simulator)

```python
import time
from pathlib import Path
from meshtastic_mcp.emulator import native_node, apple_sim

# 1. Device plane — two native nodes mesh over UDP (Linux/macOS host; see topology.md)
nodes = native_node.build_lab(Path(".pio/build/native-macos/meshtasticd"), Path("/tmp/lab"), count=2)
for n in nodes: n.start()
time.sleep(10)
for n in nodes: n.configure()           # region + EnableUDP via admin, under a restart supervisor

# 2. App plane — boot sim, install, launch Meshtastic-Apple
udid = apple_sim.ensure_booted("iPhone 17 Pro")
apple_sim.install_app("…/Meshtastic.app", udid=udid)
apple_sim.launch("gvh.MeshtasticClient", udid=udid)   # confirm the bundle id from the build

# 3. Connect the app to the DUT over TCP (UI-driven: add device → TCP/IP)
dut = apple_sim.tcp_dut_address(4403)    # "127.0.0.1:4403"
#   drive the connect sheet with apple_sim.ui_dump()/tap()/type_text(dut)

# 4. Inbound loop (device → app):
TOKEN = f"E2E-{int(time.time())}"
# tester (:4404) sends via mesh_e2e.py / send_text ...
assert apple_sim.poll_for_text(TOKEN, udid=udid, timeout=30)   # app rendered it → PASS
```

## macOS app variant (no simulator) — recommended, proven live

Fastest, reboot-free, and validated end-to-end 2026-06-25 (inbound message rendered):

```bash
xcodebuild -workspace Meshtastic.xcworkspace -scheme Meshtastic \
  -configuration Debug -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
open build/Build/Products/Debug-maccatalyst/Meshtastic.app
```

Then Connect tab → **+ Manual** → enter `127.0.0.1:4403` → OK. The app also **auto-discovers**
the node over mDNS (a "TCP" row appears), though the explicit Manual entry was more reliable.

**Driving a Catalyst app ≠ iOS Simulator:** `idb` targets iOS *simulators*, not a Catalyst app
running on the macOS desktop. Drive the macOS app with `cliclick` (coordinate clicks),
`screencapture` (screenshots), and macOS accessibility (`osascript` System Events) instead —
or a native **XCUITest** target. `apple_sim.py` (simctl + idb) is for the true iOS Simulator;
a macOS-app driver would use the cliclick/AX toolchain.

Validated flow (live): dismiss onboarding → Connect → Manual `127.0.0.1:4403` →
"Connected Radio / Subscribed" (node `0a0a`) → Messages → Channels → Primary Channel →
tester (`:4404`) broadcasts a token → **bubble renders in the app** from node `0b0b`.

## Caveats

- **Reliable element targeting needs accessibility identifiers** in the SwiftUI views (the iOS
  analog of Android `resourceId`). idb's tree exposes `AXLabel`/`AXValue`; add
  `.accessibilityIdentifier(...)` where matching is ambiguous.
- **Without `idb`**, fall back to `screenshot()` + external OCR, or a native **XCUITest** target.
- Same ACK semantics as Android (see `emulator-lab.md`): broadcast shows a delivery artifact in a
  flat virtual mesh; assert outbound on wire truth, use a DM for app-visible delivery.

## CI note

iOS Simulator + idb run on **macOS runners** (`macos-14`+). The native mesh + macOS app variant
also run there. Linux runners can host the native mesh but not the Apple app.

The repo's `ci.yml` `apple-e2e` job (manual dispatch) wires the full leg: build native-macos
`meshtasticd`, download the watchOS runtime, build `Meshtastic.app` for the simulator with ad-hoc
signing that keeps entitlements, install `idb`, and run `scripts/ci_apple_app_loop.py` (which
reuses `ci_device_mesh_e2e.mesh_up()` for the device plane). It stays manual-only until
framework-portduino#75 + firmware#10784 let a macOS runner host the UDP-multicast mesh.
