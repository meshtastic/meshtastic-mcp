# Emulator lab: hardware-free closed loop (AVD + native nodes)

> For physical Android phones, see the **Physical Android** section at the bottom.
> The device plane and all helpers are shared; only the TCP address and UI-dump path differ.

Run the full device↔app loop in software — no radios, no phone. The Android app runs in an
emulator (AVD); the mesh is `meshtasticd` native nodes on the host.

```
host / CI runner
  meshtasticd #1 (DUT)  ──┐
  meshtasticd #2 (tester)  ├─ UDP multicast mesh (224.0.0.69:4403)
       ▲                   │        ▲
  TCP :4403           TCP :4404
       │                   │
  Android emulator     MCP server (tcp://127.0.0.1:4404)
  app → 10.0.2.2:4403
```

## Building blocks (this package)

- **Device plane:** `meshtastic_mcp.emulator.native_node` — `build_lab(binary, workdir, count)`
  then per node `start()` + `configure()` (sets region + UDP via admin). See
  `topology.md` for the proven gotchas (admin-set region/UDP, restart loop, shared netns).
- **App plane:** `meshtastic_mcp.emulator.avd` — wraps the `android` CLI + `adb`:
  - `ensure_avd()` / `start(avd)` / `wait_for_boot()` — AVD lifecycle
  - `install_app(apks)` — deploy the debug APK via `android run`
  - `ui_dump(diff=…)` / `screenshot(annotate=…)` — UI oracle
  - `tap` / `type_text` / `swipe` — stimulus
  - `poll_for_text(token, timeout=…)` — bounded app-plane assertion
  - `tcp_dut_address(port)` → `10.0.2.2:<port>` — the host node the app connects to

## Recipe

```python
from pathlib import Path
from meshtastic_mcp.emulator import native_node, avd

# 1. Device plane: two native nodes mesh over UDP
BIN = Path(".pio/build/native/meshtasticd")   # Linux native (has HAS_UDP_MULTICAST)
nodes = native_node.build_lab(BIN, Path("/tmp/lab"), count=2)  # ports 4403 (DUT), 4404 (tester)
for n in nodes:
    n.start()
import time; time.sleep(10)
for n in nodes:
    n.configure()      # region US + enable UDP; under a supervisor that restarts on reboot

# 2. App plane: boot emulator, install app
name = avd.ensure_avd("medium_phone")
avd.start(name)                       # blocks until ready
serial = avd.wait_for_boot()
avd.install_app(["app-debug.apk"])    # Meshtastic-Android debug build

# 3. Connect the app to the DUT node over TCP (UI-driven: Add device → IP)
dut = avd.tcp_dut_address(4403)       # "10.0.2.2:4403"
#   drive the app's add-device flow with avd.ui_dump()/tap()/type_text(dut)

# 4. Run a loop, e.g. inbound (device → app):
TOKEN = f"E2E-{int(time.time())}"
# tester sends via the MCP send_text / mesh_e2e.py against :4404 ...
assert avd.poll_for_text(TOKEN, serial=serial, timeout=30)   # app rendered it → PASS
```

## Gotcha: outbound broadcast shows an "error" icon (NOT a delivery failure)

When the app sends a **broadcast** (channel message) in this lab, the bubble shows an error
icon even though the message is delivered. Meshtastic confirms a broadcast by hearing it
**rebroadcast by a neighbor** (implicit ACK). In a **flat UDP-multicast mesh every node hears
every other node directly (0 hops, one broadcast domain)**, so the flooding router *suppresses*
rebroadcast — nobody rebroadcasts a packet all neighbors already received. The sender never hears
an implicit ACK → `err=5` MAX_RETRANSMIT → NAK → the app paints the bubble failed. DUT log:
```
[Router] Reliable send failed, returning a nak ... id=0x...
[Router] Alloc an err=5,to=0x...           # 5 = MAX_RETRANSMIT
[Router] Received a NAK ... stopping retransmissions
```
**Adding more nodes does NOT fix this** — verified live 2026-06-24: a 3-node broadcast errored
identically (more nodes in the *same* multicast domain are still all 1 hop away, so rebroadcast
stays suppressed). `CLIENT_MUTE` nodes never rebroadcast at all, compounding it.

### Correct handling
- **Assert the outbound loop on wire truth, not the UI checkmark.** The receiving node *does*
  get the `TEXT_MESSAGE_APP` (verify via the recorder `packets_window` / a
  `meshtastic.receive.text` listener on the tester). The bubble's delivery state is a separate,
  topology-dependent signal that is unreliable in a flat virtual mesh.
- **For app-visible delivery success, send a directed message (DM), not a broadcast.** A DM gets
  a real end-to-end routing ACK *from the destination node* (not implicit-ACK-via-rebroadcast),
  which works in any topology. Verified live: a directed send drew a `ROUTING_APP` reply straight
  from the destination — mechanism confirmed. **Warm up bilateral NodeInfo first** or the reply is
  `PKI_UNKNOWN_PUBKEY` (the peers haven't exchanged public keys yet; broadcast once to exchange,
  see `harness.md` §3). With keys exchanged the routing reply is `errorReason=NONE` = delivered ✓.

## Pinning a version (test a specific fw / app)

Every leg can target an exact version, so a run reproduces a specific firmware/app:

- **meshtasticd / firmware:** `scripts/build_meshtasticd.sh --env native --ref <sha|tag|branch>`
  (or `--env native-macos`). Builds + prints `meshtasticd-sha=<sha>`.
- **Meshtastic-Android (from source):** `scripts/build_android_apk.sh --ref <sha|tag|branch>`
  → a debug APK; prints `android-sha=<sha>`. Or pin a published release with the workflow's
  `android_apk_ref`.
- **Meshtastic-Apple:** the `apple-e2e` job's `apple_ref` input.

In CI these map to the `workflow_dispatch` inputs `firmware_ref` / `android_ref` /
`android_apk_ref` / `apple_ref` (blank = default branch / latest release). The resolved sha is
written to the job summary, and `ci_device_mesh_e2e.py` stamps the DUT firmware version into its
verdict (`fw=…`) so results are traceable to versions.

## CI note

Run on a **Linux** runner: Linux native has `HAS_UDP_MULTICAST=1` by default and multicast
loopback is well-supported. macOS local dev needs the framework-portduino Darwin bind fix
(meshtastic/framework-portduino#75) + the native-macos UDP flag (meshtastic/firmware#10784).
BLE is not emulatable — the emulator path is TCP-only; BLE stays a hardware-tier concern.

The repo's `ci.yml` wires this (manual dispatch + weekly schedule):
- **`meshtasticd-native`** — builds the native binary once, uploads it as an artifact.
- **`device-mesh-e2e`** — the deterministic device-plane loop via `scripts/ci_device_mesh_e2e.py`
  (build + supervise + configure + assert receipt over TCP). Self-contained must-pass signal.
- **`android-e2e`** — the full app loop via `scripts/ci_android_app_loop.py` on
  `reactivecircus/android-emulator-runner`; depends on the `android` dev CLI for UI drive.
- **`apple-e2e`** — the iOS-Simulator app loop via `scripts/ci_apple_app_loop.py` on a macOS
  runner; manual-only until #75 / #10784 let a macOS runner host the multicast mesh.
All three app helpers share `mesh_up()` and the `LOOP … PASS|FAIL` verdict line.

## Verdict format

Per loop: `LOOP <name> <PASS|FAIL> token=<…> latency=<ms>` — same as the hardware loops, so
the emulator tier and the hardware tier report identically.

## Physical Android (USB phone)

Same device plane as the emulator lab. Replace the AVD steps:

```python
from meshtastic_mcp.emulator import avd

# 1. Confirm the phone is connected and trusted
serial = avd.find_device_serial(physical_only=True)  # e.g. "R5CT80XXXXX"
assert serial, "no physical Android device found — check `adb devices`"

# 2. Install the app (adb install, not android run)
avd.install_app("app.apk", serial=serial)

# 3. Set up reverse tunnel + connect the app to meshtasticd on the host
host = avd.tcp_dut_address(port=4403, serial=serial)  # sets up adb reverse
avd.connect_app_to_tcp(host=host, serial=serial)

# 4. Run loops as normal — all helpers are device-agnostic
avd.poll_for_text("Disconnect", serial=serial, timeout=30)
avd.screenshot("/tmp/screen.png", serial=serial)
```

Prerequisites: USB debugging enabled on the phone, device trusted (`adb devices` shows
`device` not `unauthorized`), and meshtasticd running on the host at the forwarded port.
