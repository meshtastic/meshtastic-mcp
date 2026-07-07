---
name: meshtastic-e2e
license: GPL-3.0-only
description: Closed-loop end-to-end testing of a Meshtastic device and a Meshtastic app (Android or Apple) together. Use when validating that an action on the radio (send a text, change config, a node beacon, a power-cycle) surfaces correctly in the app UI, or that an action in the app produces the correct on-air/on-device result. Drives the firmware device via the Meshtastic MCP server and the app via the Android CLI (`android` + `adb`) or the Apple toolchain (`xcrun simctl` + `idb`), then cross-asserts one plane against the other.
---

# Meshtastic Device ↔ App E2E Testing

Two independent planes, each with its own **stimulus** and **oracle**. A closed-loop
test stimulates one plane and asserts on the *other*.

| Plane | Stimulus | Oracle (observation) |
|---|---|---|
| **Device** (Meshtastic MCP) | `send_text`, `set_config`, `push_fake_nodedb`, `send_input_event`, `uhubctl_cycle`, traceroute | recorder `packets_window` / `telemetry_timeline` / `logs_window` / `events_window`, `device_info`, `list_nodes`, serial logs, `capture_screen`+OCR |
| **App — Android** (android CLI + adb) | `adb shell input tap/text/swipe`, `android run` | `android layout --diff`, `android screen capture [--annotate]` |
| **App — Apple** (xcrun simctl + idb) | `idb ui tap/text`, `simctl install/launch` | `idb ui describe-all`, `simctl io screenshot` |

## Reference files (load the one you need)

- `references/topology.md` — two-radio wiring, app-over-TCP, the uhubctl USB switch, preflight.
- `references/harness.md` — the hard rules (marker tokens, bounded polling, recorder oracle, adb nav primitives, verdict format). **Read this before any loop.**
- `references/mesh_e2e.py` — runnable device-plane helper (`devices`/`info`/`send`/`recv-text`/`watch-tx`/`traceroute`/`recorder`); emits grep-able `PASS …`/`FAIL …` lines.
- `references/loop-inbound.md` — device → app message render
- `references/loop-outbound.md` — app → device wire truth
- `references/loop-node-sync.md` — node appears in app node list
- `references/loop-config-writeback.md` — app setting persists on device (RAM + NVS)
- `references/loop-resilience.md` — uhubctl power-cycle fault injection
- `references/emulator-lab.md` — hardware-free Android: AVD app over TCP + native UDP mesh
- `references/replay-app-features.md` — **drive app features (geofence/waypoints/alerts) by injecting exact packets via the replay engine** (`replay_inject`); logcat/notification oracles + gotchas
- `references/simulator-apple.md` — hardware-free Apple: iOS Simulator / macOS app over TCP (`127.0.0.1`) + native UDP mesh
- `references/journeys.md` — **journey-driven UI (recommended over hardcoded coordinates)**: drive the app from a goal via the live a11y tree, version-resilient. Ships journey XML in `references/journeys/`.
- `references/vision-oracle.md` — use a screenshot + vision as the assertion when the a11y tree is empty (WebView/Canvas/animation).
- `references/triage.md` — dual-plane root-cause analysis of a FAIL (pairs with the `triage_e2e_failure` MCP prompt).

The device plane (`mesh_e2e.py`, native nodes, recorder) is platform-neutral and shared; only
the app plane differs (Android CLI+adb vs Apple `xcrun simctl`+`idb`).

## Topology (read first)

Three supported topologies — pick the one that matches your hardware:

| Topology | Device plane | App plane | DUT address |
|---|---|---|---|
| **Emulator lab** (no hardware) | `meshtasticd` native nodes via UDP multicast | Android AVD | `10.0.2.2:<port>` |
| **Physical Android** | Real radios via USB serial | USB-attached Android phone | `adb reverse` → `127.0.0.1:<port>` |
| **Physical Apple** | Real radios via USB serial | iOS Simulator only (see caveat) | `127.0.0.1:<port>` |

For **physical Android**: `avd.tcp_dut_address(port, serial=<phone_serial>)` sets up the
`adb reverse` tunnel automatically and returns the correct address. Pass it to
`connect_app_to_tcp()`. UI observation falls back from `android layout` to
`adb exec-out uiautomator dump` transparently — all helpers (`poll_for_text`, `find_text`,
`_tap_text`) work on both emulators and physical phones.

> **iOS physical devices — not supported.** `apple_sim.py` targets the iOS Simulator only
> (`xcrun simctl`, `idb`). Physical iPhone/iPad requires code signing + provisioning profiles,
> `libimobiledevice`/`usbmuxd` for device comms, and XCTest/XCUITest for UI automation
> (idb is simulator-only). This is a separate, significant project — not a configuration change.

Full wiring detail and the single-radio app-over-TCP workaround in `references/topology.md`.
One radio cannot be both tester and DUT (the serial port lock is exclusive).

## Prerequisites

Before running any loop, verify all of these. Missing items cause silent failures, not clear errors.

| Requirement | Check | How to fix |
|---|---|---|
| `MESHTASTIC_FIRMWARE_ROOT` set | `echo $MESHTASTIC_FIRMWARE_ROOT` | Set to your firmware checkout path |
| Two radios (TESTER + DUT) | `list_devices()` → ≥2 ports with `likely_meshtastic=true` | Plug in both radios; use TCP DUT if only one physical radio |
| App installed on device/emulator | `adb shell pm list packages \| grep meshtastic` | `android run` or `adb install` |
| Recorder running (process-global) | `recorder_status()` → `running=true` | Auto-starts on first MCP serial call; captures every interface |
| `uhubctl` available (Loop 5 only) | `uhubctl -l` returns hub info | `brew install uhubctl`; set `MESHTASTIC_UHUBCTL_LOCATION_TESTER` |
| `doctor()` returns `ok=true` | Call `doctor()` | Run each `fix_commands` entry in order |

## Bootstrap

```bash
export MESHTASTIC_FIRMWARE_ROOT="$HOME/meshtastic/firmware"
MCP="$MESHTASTIC_FIRMWARE_ROOT/mcp-server/.venv/bin/python"
S="$HOME/.agents/skills/meshtastic-e2e/references/mesh_e2e.py"
$MCP "$S" devices               # list tester radios
TESTER=/dev/cu.usbmodem101       # pick one
adb devices                     # confirm the phone (DUT) is attached
adb shell pm list packages | grep meshtastic   # confirm the app is installed
```
The MCP server (registered as user-scope `meshtastic`) exposes the 53 tools directly when
running inside Claude Code; the `$MCP "$S" …` helper is the standalone/CI path.

## Hard rules (mesh is async + lossy — respect these or get flaky tests)

1. **Marker token per message.** Never assert on a bare "hello". Embed a unique token
   (`E2E-$(date +%s)-$RANDOM`) so a busy 80-node mesh can't false-positive your grep.
2. **Bounded polling, never `sleep N` then assert once.** Poll every 1 s up to a deadline.
   Use these ceilings: single-hop broadcast → 20 s; directed/PKI send → 30 s; multi-hop
   (≥2) → 45 s. Mesh delivery is best-effort; these cover the 99th-percentile lab case.
3. **Warm up directed/PKI sends.** Directed + encrypted sends need bilateral NodeInfo
   (both sides hold each other's current pubkey). Broadcast (`^all`) first to exchange,
   or send to a node already in both DBs.
4. **The recorder is the device-side source of truth.** It timestamps every RX packet to
   JSONL; align those timestamps with `android layout` snapshots. Start it before the
   stimulus, query the window after.
5. **One MCP call per serial port at a time** (exclusive lock): open → act → close.
6. **`layout` can fail on WebView/animation** — fall back to `screen capture --annotate`
   + visual/OCR inspection.

## Loop 1 — inbound message (device → app)

Stimulate from the tester radio; assert the bubble renders in the app.

```bash
TOKEN="E2E-$(date +%s)"
# 1. device stimulus: tester radio broadcasts (or directs to the DUT node)
$MCP -m meshtastic_mcp.cli ...   # or drive send_text via the meshtastic API:
$MCP -u -c "
import meshtastic.serial_interface as si
i=si.SerialInterface('$TESTER'); i.sendText('$TOKEN', wantAck=False); i.close()"
# 2. app oracle: poll the UI tree for the token (bounded)
for t in $(seq 1 30); do
  android layout 2>/dev/null | grep -q "$TOKEN" && { echo "PASS: rendered"; break; }
  sleep 1
done
```
Open the messages screen in the app first (`adb shell input` to navigate, or `android run`
to the messages activity). Use `layout --diff` to keep only the changed bubble in context.

## Loop 2 — outbound message (app → device)

Type+send in the app; assert the wire truth on the tester radio's recorder.

1. Start the recorder, then drive the app:
   ```bash
   # ensure recorder is capturing (it auto-starts when the MCP server is live;
   # standalone: open a SerialInterface to TESTER and call get_recorder().start())
   ```
2. App stimulus — focus the compose field, type the token, tap send:
   ```bash
   android layout --pretty | jq '.[] | select(.interactions|index("focusable"))'  # find input
   adb shell input tap <cx> <cy>          # focus compose field (must show "focused")
   adb shell input text "$TOKEN"
   adb shell input tap <send_cx> <send_cy>
   ```
3. Device oracle — the tester must *receive* it; query the recorder window:
   ```bash
   $MCP -c "from meshtastic_mcp import log_query as q,json;
   print(json.dumps(q.packets_window(max=20)))" | grep -i "TEXT_MESSAGE_APP"
   ```
   Decode the payload hex / matched text for `$TOKEN`. PASS when a `TEXT_MESSAGE_APP`
   packet from the DUT node carrying the token lands within the deadline.

## Loop 3 — node sync (device → app)

`push_fake_nodedb` (or a real beacon) on the DUT radio → assert the node appears in the
app's node list (`android layout` over the nodes screen, match long_name/short_name).
Backstop the device truth with `list_nodes`.

## Loop 4 — config write-back (app → device)

Change a setting in the app (e.g. region, device role, a channel name) → assert it
persisted on the DUT radio:
```bash
$MCP -c "from meshtastic_mcp import admin,json; print(json.dumps(admin.get_config('lora', port='$DUT')))"
```
Reboot the radio (`reboot` MCP tool) and re-read to prove NVS persistence, not just RAM.

## Loop 5 — resilience / fault injection

Mid-conversation, power-cycle a relay or the peer with `uhubctl_cycle` (needs `uhubctl`):
assert the app shows the node go **offline → online** and that a queued message recovers
once the path heals. This is the app-facing mirror of the firmware suite's
`test_peer_offline_recovery`.

## Loop 6 — ATAK/iTAK render (sim TAK squad → TAK client)

Validates the TAK plane. Meshtastic apps (≥2.8) bridge mesh TAK traffic to a
connected ATAK/iTAK client via an **in-app local TAK server** that emits CoT
(the deprecated `IMeshService` plugin is gone). Two layers:

- **Bridge-semantics (no emulator, in CI):** the sim emits a TAKPacketV2 squad
  (`replay_start(source=..., sim_profile={"tak": {"team_nodes": N, "wire": "v2"}})`
  — v2 rides portnum 78 `ATAK_PLUGIN_V2`; v1 rides 72). `replay/tak_server.py`
  `capture_to_cot_events()` reproduces the bridge's wire→TAKPacketV2→CoT path;
  `tests/unit/test_tak_bridge.py` asserts the CoT is well-formed, typed
  (`a-f-G-U-C` PLI), and carries the right callsign/position/GeoChat. Needs the
  `[tak]` extra (meshtastic-tak SDK).
- **App-plane (opt-in, needs an emulator + ATAK-CIV), bidirectional:**
  `scripts/ci_atak_app_loop.py` stands up `CotTakServer` from that squad, points
  ATAK-CIV at `10.0.2.2:<port>` (a pushed streaming-input `.pref`), launches it,
  and asserts (receive) a squad callsign marker renders **and** (send) ATAK's
  own self-PLI streams back, is captured, and converts to a mesh TAKPacketV2.
  ATAK-CIV is free (`com.atakmap.app.civ`, Play Store / tak.gov / GitHub; needs
  GLES 3.0). Emits `LOOP atak-render …` + `LOOP atak-send …`. iTAK is iOS-only
  and App-Store-distributed, so in-simulator automation isn't practical —
  physical device only.

Both directions are unit-tested without hardware: `test_tak_bridge.py` covers
receive (mesh→CoT) and send (`cot_to_wire`, CoT→mesh); `test_tak_server.py`
asserts the server streams to a client **and** captures a client-authored CoT.

You can also point a real ATAK/WinTAK at `CotTakServer` directly (host:port,
plain TCP streaming input) to eyeball the sim's squad on a live map.

## Reporting

Emit a compact verdict per loop: `LOOP <n> <PASS|FAIL> token=<...> latency=<ms> hops=<n>`.
On FAIL, attach: the app `layout`/`screen capture` at deadline, and the recorder
`packets_window` + `logs_window` tail for the same wall-clock window (they share epoch
timestamps — that alignment is the whole point of the dual-plane design).

## Related

- Firmware-only device UI testing (OLED via `send_input_event` + camera/OCR): firmware
  repo `mcp-server/tests/ui/` and the `/test` slash command.
- App-only UI tests: `android` journeys (`references/journeys.md` in the `android-cli` skill).
- This skill is the **cross-plane** layer that neither of those covers.
