# Session Context — meshtastic-mcp

> `git@github.com:meshtastic/meshtastic-mcp.git` · durable working context (gotchas + blockers).

## What this repo is

Standalone MCP server + bundled agent skills for AI tooling to discover, drive, observe, and
test Meshtastic devices and apps. Extracted from `firmware/mcp-server`; publishable as
`meshtastic-mcp` on PyPI. See `AGENTS.md` for architecture, `README.md` for install/usage.

## Proven live

All three hardware-free e2e loops have been validated end-to-end on macOS:

| Loop | Path | Status |
|------|------|--------|
| Device-plane mesh | tester `meshtasticd` :4404 → UDP multicast → DUT :4403 | ✅ PASS `CI-E2E-…` |
| Android emulator | app ⇄ TCP `10.0.2.2:4403` ⇄ mesh | ✅ PASS (avd.connect_app_to_tcp) |
| iOS Simulator | app ⇄ TCP `127.0.0.1:4403` ⇄ mesh | ✅ PASS `CI-APPLE-…` |
| macOS Catalyst | app ⇄ TCP `127.0.0.1:4403` ⇄ mesh | ✅ PASS (earliest proven path) |

The native-macos mesh requires the framework-portduino Darwin multicast fix (PR #75 below).
The CI `apple-e2e` job is wired and ready; it stays manual-only until #75 lands.

## Open upstream PRs (blockers)

- **meshtastic/framework-portduino#75** — *Fix Darwin multicast loopback by binding to INADDR_ANY*.
  Blocks `apple-e2e` in CI (macOS runners can't host the multicast mesh without it).
  Fork: `jamesarich/framework-portduino`, branch `fix/darwin-multicast-loopback-bind`. DCO-signed.
- **meshtastic/firmware#10784** (DRAFT) — *build(native-macos): enable UDP multicast mesh*.
  Adds `-DHAS_UDP_MULTICAST=1` to native-macos build_flags. Blocked on #75 + platform-native bump.

To land: merge #75 → bump `platform-native` SHA in firmware → un-draft #10784.

## Hard-won gotchas (baked into docs/code, recorded here for context)

### iOS Simulator build
- `CODE_SIGNING_ALLOWED=NO` strips entitlements → `INPreferences`/Siri crash on launch.
  Build with `CODE_SIGN_IDENTITY="-" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=YES
  AD_HOC_CODE_SIGNING_ALLOWED=YES`. Diagnose crashes with `simctl launch --console-pty`.
- watchOS runtime "not showing up": root cause is **duplicate disk images** (not a missing
  reboot). `xcrun simctl runtime delete` all watchOS entries, re-download once.
- `idb_companion`: install from `facebook/fb` tap (`brew trust facebook/fb &&
  brew install facebook/fb/idb-companion`). The `companion` cask is unrelated.
- `fb-idb` client breaks on Python 3.14 (`asyncio.get_event_loop` removed). Pin to 3.12:
  `pipx install --python $(brew --prefix)/bin/python3.12 fb-idb`.

### iOS Simulator UI drive (ci_apple_app_loop.py)
- Tab bar items (Messages, Connect) are **not labeled** in idb's flat a11y tree. Use
  coordinate constants `_TAB_MESSAGES_X/Y`, `_TAB_CONNECT_X/Y`.
- The "Connected Radio" hint callout after first connect **overlays the tab bar**, hiding
  tab items from the tree. Navigate to Primary Channel **before** connecting; return via
  coordinate tap after confirming "Subscribed".
- `_element_center("Allow", …)` matched StaticText bodies before Buttons. Fixed: interactive
  types score priority 0, StaticText priority 1 in candidates list.
- Permission dialogs (location-always, notifications, Siri) appear at any point including
  during mesh startup (~32s). `_dismiss_pending()` is called in the connection poll loop.

### native_node.build_lab
- Node 0 got MAC `000000000000` → firmware rejects as "Blank MAC Address" → port never binds
  → supervisor loops forever. Fixed: MACs are `DE{idx:010X}`, quoted in YAML template.

### idb companion lifecycle
- `idb_companion --udid <UDID>` must be started, then `idb connect 127.0.0.1 <port>` run
  to register it. Stale entries from previous sessions cause "Connection refused".
  `apple_sim.start_companion(udid)` handles this end-to-end (parses gRPC port from stdout,
  disconnects stale entry, registers). Call it after `ensure_booted`.

## Notable design points

- **`provision` + `doctor`:** `meshtastic-mcp doctor` reports/acquires binaries; `provision`
  clones missing firmware/android/apple source trees (`MESHTASTIC_{FIRMWARE,ANDROID,APPLE}_ROOT`)
  and writes a `.env`.
- **Version-pinned builds:** `scripts/build_{meshtasticd,android_apk,apple}.sh --ref <sha|tag>` +
  the CI `firmware_ref`/`android_ref`/`apple_ref` inputs; verdicts stamp the DUT firmware version.
- **Skills:** `meshtastic-device-ops` (tool surface) + `meshtastic-e2e` (cross-plane testing,
  journey-driven UI, triage, vision oracle).
- **`factory_reset(full=True)`** sets `factory_reset_device` (wipes BLE bonds + identity key);
  `full=False` sets `factory_reset_config` (keeps them). The firmware dispatches on *which* field
  is set, not the value — a past bug.

## Pending

- **PyPI Trusted Publishing:** enable in PyPI project settings (release.yml is ready).
- **framework-portduino #75 + firmware #10784:** merge to unblock `apple-e2e` in CI.

## Environment notes (macOS dev)

- The native-macos `meshtasticd` mesh needs the #75 Darwin multicast bind patch applied to the
  local framework-portduino package (built with `HAS_UDP_MULTICAST=1`). Linux native works as-is.
- Run `meshtastic-mcp doctor` for the live dependency/capability report and exact install commands
  (idb_companion from the `facebook/fb` tap; `fb-idb` under Python ≤ 3.12; the `android` CLI).
- `meshtastic-mcp provision` clones missing firmware/android/apple source trees and writes a `.env`.
