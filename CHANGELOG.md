# Changelog

All notable changes are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com); versions follow SemVer.

## [0.1.0] — Unreleased (first public release)

### Added
- **FleetSuite — web control plane + hardware bench harness** (#9) — a FastAPI backend
  (`[web]` extra, `meshtastic-mcp-web` entrypoint) + Vue 3 SPA for running a multi-board USB
  bench: live device registry with discovery + auto-enrichment, build queue + flash, an
  escalating recovery ladder (reboot → USB power-cycle → 1200bps touch → reflash), per-device
  camera streams, serial monitors, the tiered test runner, and optional Datadog shipping.
  Lifted from `meshtastic/firmware`'s `mcp-server/` tree and reconciled onto this repo's
  capability-gated core; includes the hub-slot-keyed per-board bench roles
  (`tests/_bench.py`), `port_recovery.py`/`recovery.py`, and the bake/mesh/telemetry/monitor/
  fleet/admin/provisioning/recovery/ui hardware test tiers.
- **RF-compliance oracle via RTL-SDR** (#8) — `rf_scan` / `rf_confirm_tx` tools (`[sdr]`
  extra): spectrum occupancy checks and independent on-air TX verification.
- **Replay answers `get_owner_request`** — the simulated device now emulates the firmware's owner
  admin round-trip (replies with the owner `User` + a session passkey, echoing the request id), so
  strict clients that block their post-NodeDB "seeding" step on it can reach a ready/connected
  state. Previously the replay device only handled `want_config_id`, so such a client drained
  config + the full NodeDB and then timed out. Read-only still holds: mutating admin writes are
  not honored (replay has tx disabled).
- **Kotlin-SDK device-IO bridge (experimental)** — an optional capability that drives a device
  through the Meshtastic Kotlin SDK's headless JVM sample CLI instead of the Python `meshtastic`
  library, by shelling out to it in `--json` (NDJSON) mode — the same pattern the MCP already uses
  for `pio`/`adb`/`idb`/`esptool`. Tools `sdk_status` / `sdk_device_info` / `sdk_list_nodes`
  (read-only) + `sdk_send_text` (destructive), gated on a resolvable `cli` launcher
  (`MESHTASTIC_MCP_SDK_CLI` or `MESHTASTIC_SDK_ROOT`). Proof-of-concept for evaluating the SDK
  engine (BLE/TCP/serial, handshake, NodeDB, ACK correlation) as an alternative backend. See
  `docs/sdk-cli-bridge.md`.
- **Local-model offload** (`summarize_window` / `vision_oracle` / `triage_window`, plus
  `local_model_status` / `local_model_serve` / `local_model_serve_stop`) — an optional capability
  that pushes token-heavy work onto a local GPU: distill a recorder window, a first-pass
  e2e-failure triage, and an offline vision oracle for screenshot assertions when the a11y tree is
  empty. Backend-agnostic (a local Ollama or any OpenAI-compatible `llama-server`), defaults to
  Gemma 4, and can start the self-contained llama.cpp server itself. Tools register only when a
  backend is reachable; local output is treated as an untrusted draft. See `docs/local-models.md`.
- **`install` / `uninstall` CLI** — one step to register (or remove) the server in an MCP client's
  `mcpServers` config and install the bundled skills. Picks the right config per `--client`
  (claude-code / claude-desktop / cursor / windsurf) and `--scope` (user/project), or `--config
  PATH`; edits JSON in place preserving other entries; `--local` registers the current interpreter
  (editable/dev), `--print` emits the snippet, `--env KEY=VALUE` bakes in capability vars. Adds a
  matching `skills uninstall`.
- **Replay engine** (`replay_start` / `replay_status` / `replay_stop`) — the inverse of the
  recorder: serve a capture as a simulated Meshtastic TCP device. An app/AVD connects to the
  listen port, does the want-config handshake, and receives a paced packet stream restamped to
  "now". Source-agnostic (`replay/capture.py`): full-fidelity SQLite captures (Burning Man /
  DEF CON / MeshCon schema), best-effort recorder `packets.jsonl`, or an in-memory synthetic
  mesh. Tunable pacing (`speed`/`rate`/`max_gap`), windowing (`start`/`end`), `loop`, node-DB cap.
- **Synthetic mesh generator** (`replay/sim.py`, MeshCon @ the VLA) — seeded, PII-free, with a
  diurnal activity envelope and every portnum/flavor (incl. RANGE_TEST), driven by a
  statistics-driven `PROFILE`. The default distributions (hardware/role mix, telemetry value
  ranges — battery, channel-utilisation tail, air-util — position precision, hop-limit spread, text
  rate) are informed by the aggregate statistics of real ~1,800-node captures (Burning Man +
  the combined DEF CON 33 capture). Also models **node presence/churn** (a persistent
  infrastructure core plus transient attendees who arrive, beacon briefly, and leave) which
  reproduces the real heavy activity skew (top ~1% of nodes carry ~a third of traffic; median ~19
  packets/node), **short-message** length distribution, and a share of **encrypted/foreign**
  traffic the viewer can't decode. Scales to thousands of nodes (no node-count cap on the
  generator; `limit_nodes` only caps the handshake node-DB like a real radio). Only proportions inform the sim — all identities, positions, and
  messages are generated. `sim.fit_profile(capture)` derives such a profile from any capture.
- **Replay live injection** (`replay_inject`, `replay/build.py`, `capture.from_events`): push exact
  packets into a running session to drive app features on demand — a geofence waypoint, a node
  position crossing it, a text, a NodeInfo, or `raw`. Packet builders set proto fields the bundled
  `meshtastic` lib predates (e.g. Waypoint geofence fields) via raw-wire append; `fuzz=True` runs
  the injected packet through the session's fuzz mutator (inject a deliberately malformed trigger),
  sharing the fuzzer's emit path. `port=0` auto-picks a free port and a clear `PortInUseError`
  replaces a silent hijack when the port is taken. Validated against the Android waypoint-geofence
  PR. New app-plane oracles `poll_logcat` / `poll_notification` / `read_logcat` in `emulator/avd.py`.
- **Replay app-facing polish**: the connected node is placed at the capture's median position
  (sane map + node distances); `announce_interval` adds a "Replay Clock" node posting a kickoff and
  live "ETA — done/total" progress to the busiest channel; `modem_preset` / `firmware_edition` set
  the advertised LoRa preset and the app's event banner; `replay_status` returns `connect`
  host:port hints; a socket send timeout keeps a stalled app from hanging a session.
- **Channel-hash routing + key advertising** (`replay_start(channels=[…])`,
  `from_sqlite(channel_specs=…)`) — route packets into caller-supplied named channels by their OTA
  channel hash and advertise those channels' PSKs, so a connecting app shows the true channels and
  live-decrypts the still-encrypted half of the stream. Channel set is plain data (name + PSK +
  optional explicit hashes / catch-all bucket); nothing event-specific is baked in.
- **Fuzzing / adversary layer** (`replay/fuzz.py`, `replay_start(fuzz=…)`, `replay_fuzz_presets`)
  — turn the replay stream hostile to test app/decoder robustness. *Protocol fuzzing*: corrupt /
  truncate / garbage payloads, portnum↔body mismatch, invalid-UTF-8 text, impossible telemetry,
  teleporting positions, hop anomalies, drop/duplicate. *Bad-actor campaigns*: evil-twin
  impersonation, flooding, GPS spoofing, forged routing ACKs, rogue ADMIN (reboot/factory-reset)
  packets at the connected app, malicious waypoints. Seeded (a crash reproduces); activity
  reported under `fuzz` in `replay_status`. Presets: `light`, `parser`, `adversary`, `chaos`.
- **Async flash** (`flash_start` / `flash_poll`) mirroring the build pair — the upload step
  also exceeds the 60 s MCP timeout. Both now share a generic background-job runner.
- **Config snapshots + diff** (`config_snapshot`, `config_snapshots_list`, `config_diff`):
  capture a device's full config to a named snapshot, then diff two snapshots or a snapshot
  vs the live device (field-level, dot-path keyed). Useful for firmware-upgrade verification.
- **`send_text(wait_for_tx=True)`** — optionally polls the recorder for the matching
  TEXT_MESSAGE_APP packet, collapsing send + confirm into one call.
- **Live device-state MCP resources** `meshtastic://device/info` and `meshtastic://device/nodes`.
- **CLI `watch`** — live-tail recorder streams (logs/packets/events).
- **CLI `completion`** — prints bash/zsh completion scripts.

### Fixed
- **Base install works with no extras** — `server.py` → `rf_oracle`/`sdr` imported numpy at
  module level, so `pip install meshtastic-mcp` couldn't start the server without the `[sdr]`
  extra. numpy is now lazy, and capability detection can no longer crash startup on a broken
  SDR stack (e.g. an old system `librtlsdr`).
- **FleetSuite degrades gracefully without a firmware root** (#10) — auto-enrichment records
  fw/hw/region with the pio env unresolved instead of silently aborting;
  `POST /devices/{serial}/refresh` and `GET /api/boards` answer 409 with the reason instead
  of a raw 500; an app-wide `ConfigError` handler covers any firmware-dependent lookup.
- **Docker image builds again** — `.dockerignore` excludes `.git`, which broke hatch-vcs
  version detection; the build now takes `--build-arg VERSION=`.
- The wheel ships the built FleetSuite SPA (`web/static`); the release workflow builds it
  and refuses to publish from a non-tag run.
- User-visible strings no longer reference the pre-extraction `mcp-server/` layout
  (reproducer bundles, camera install hint, TUI capture paths).

### Changed
- SPDX license headers stamped across the FleetSuite/web, web-ui, and test suites (GPL-3.0-only).
- `MESHTASTIC_MCP_DD_SITE` overrides FleetSuite's Datadog site (default `us5.datadoghq.com`).
- Auto-load `MESHTASTIC_*` env vars from `~/.config/meshtastic-mcp/.env` (CLI works in
  non-interactive shells without manual sourcing).
- esptool auto-discovery from the PlatformIO penv — no manual `MESHTASTIC_ESPTOOL_BIN` wrapper.
- Tool annotations: full coverage (every tool classified), `_IDEMPOTENT_WRITES`,
  android/apple capability gating, lethal-trifecta `openWorldHint` on `logs_window`/`packets_window`.
- `doctor` now detects uhubctl udev-permission issues on Linux with the exact fix command.
- **Device discovery recognizes common board chips.** The upstream allowlist is only `0x239a`
  (Adafruit/nRF) + `0x303a` (Espressif native-USB / USB-Serial-JTAG), so it flagged the USB-UART
  bridges most ESP32 boards ship with as `likely_meshtastic=False`. `list_devices` now also treats
  CP210x (`0x10c4`, Heltec/most ESP32), CH340/CH9102 (`0x1a86`), FTDI (`0x0403`), and Seeed XIAO
  (`0x2886`, TinyUSB-CDC) as likely, and bases the flag on the VID directly rather than the upstream
  `findPorts` filter (which hid a bridge-chip board whenever a native-USB board was also attached).

## [0.1.0-pre] — 2026-06-25 (internal extraction milestone, never published)

Initial release.


### Added
- **Second bundled skill `meshtastic-device-ops`** — agent guide to the MCP tool surface
  (discover/connect/configure/observe/recover/flash); complements `meshtastic-e2e`. Both ship
  in the wheel and install via `meshtastic-mcp skills install`.
- **Journey-driven UI testing** for the e2e skill: `references/journeys.md` + platform-neutral
  journey XML (`references/journeys/{inbound,outbound,node-sync}.journey.xml`) an agent evaluates
  against the live a11y tree — version-resilient, retires hardcoded tap coordinates. Plus
  `references/triage.md` (dual-plane root-cause buckets) and `references/vision-oracle.md`
  (assert from pixels when the a11y tree is empty).
- **More MCP tools:** `android_version_lookup` (latest maven/Android versions),
  `android_render_compose_preview` (render a @Preview to PNG, no emulator), `triage_bundle`
  (one-call recorder window: packets+logs+events for a failure window).
- **MCP prompt `compare_firmware`** — the firmware-PR impact workflow over the version-pinned
  build helpers.
- **Evals: tool-selection + trajectory tiers.** `.github/evals/tool-selection.csv`
  (`intent,expected_tool`) scored for selection accuracy; `canonical-tasks.md` gains
  `select`/`knowledge`/`trajectory` (incl. a Tier-1 CI-runnable tier). `test_evals_dataset.py`
  guards the dataset against tool renames/removals.
- **MCP resources + prompts** (not just tools): resources `meshtastic://doctor`,
  `meshtastic://capabilities`, and templated `meshtastic://e2e/{loop}` (serves the bundled
  loop recipes, path-traversal-guarded); prompts `triage_e2e_failure`, `bringup_device`,
  `inbound_loop`.
- **`android_docs_search` / `android_docs_fetch` tools** — grounded Android/Compose/API answers
  from the official Knowledge Base via the `android docs` CLI (read-only, open-world).
- `build_android_apk.sh` now resolves the built APK via **`android describe`** (authoritative
  build-target metadata) and only globs `build/outputs/apk` as a fallback.
- Standalone extraction of the Meshtastic MCP server from the firmware repo, with a portable
  core + gated `firmware`/`emulator`/`apple` capabilities.
- `doctor` MCP tool + `meshtastic-mcp doctor [--json]` CLI: probes every external dependency
  and prints the exact, platform-aware command to acquire anything missing/degraded (detects
  e.g. `fb-idb` running under an incompatible Python by inspecting the `idb` script's own
  interpreter).
- Apple app-plane (`emulator/apple_sim.py`) hardware-free e2e: iOS Simulator + macOS-app
  control via `simctl`/`idb`; full inbound device→app loop validated on the true iOS Simulator.
- Hardware-free e2e CI tier (`ci.yml`, manual dispatch + weekly schedule): `meshtasticd-native`
  build, `device-mesh-e2e` (deterministic device-plane loop), `android-e2e` (AVD app loop via
  `reactivecircus/android-emulator-runner`), and `apple-e2e` (iOS-Simulator app loop on a macOS
  runner; manual-only pending framework-portduino#75 / firmware#10784). Driven by committed,
  unit-tested helpers sharing `mesh_up()` + the `LOOP … PASS|FAIL` verdict: `ci_device_mesh_e2e`,
  `ci_android_app_loop`, `ci_apple_app_loop`.

- Version-pinned e2e: build/test a specific firmware or app version. `workflow_dispatch` inputs
  `firmware_ref` / `android_ref` / `android_apk_ref` / `apple_ref`; build helpers
  `scripts/build_meshtasticd.sh` (native / native-macos) and `scripts/build_android_apk.sh`
  (from source, or download a release). The resolved sha lands in the job summary and the
  device-plane verdict stamps the DUT firmware version (`fw=…`).
- `MESHTASTIC_ANDROID_ROOT` and `MESHTASTIC_APPLE_ROOT` env vars in `config.py`: devs point
  them at existing checkouts; build scripts use them as default `--source-dir`. Parallel to the
  existing `MESHTASTIC_FIRMWARE_ROOT`. Surfaced in `doctor` as `android-source` / `apple-source`
  checks with `git clone` fix commands when absent.
- `scripts/build_apple.sh`: standalone iOS-Simulator `.app` builder matching the pattern of
  `build_meshtasticd.sh` / `build_android_apk.sh`. Takes `--ref`, `--source-dir` /
  `MESHTASTIC_APPLE_ROOT`, `--sim`, `--dest`; handles watchOS runtime download; emits
  `apple-sha=<sha>`. CI `apple-e2e` job now uses it instead of inline `xcodebuild`.
- `meshtastic-mcp provision` CLI subcommand: clones missing repos (firmware/android/apple) to
  the platformdirs data dir, respects already-set env vars, writes a sourceable `.env` file.
  Complements `doctor` (binaries) with repo-tree setup for fresh environments.

### Fixed
- `factory_reset(full=True)` now sets `factory_reset_device` rather than `factory_reset_config`,
  so a full wipe clears BLE bonds and the X25519 identity key. Regression-tested.
- `serial_open` acquires the port lock and checks for an active session atomically, closing a
  race where two concurrent opens could both own one port.
- Bounded subprocess timeouts (60 s) in `emulator/avd.py` and `emulator/apple_sim.py` so a
  wedged `adb`/`idb` daemon can't hang a tool or e2e loop.
- uiautomator XML parsing allows negative bounds (partially off-screen controls keep a tappable
  center) and raises on malformed XML instead of returning an empty tree.
- Physical-device `screenshot` writes atomically; `start_companion` cleans up on a failed
  connect; `native_node.build_lab` generates non-zero, distinct, quoted MACs (an all-zero MAC
  was rejected by firmware as blank, breaking mesh bring-up).
- `recorder_export` is annotated `destructiveHint` (it writes under a client-supplied dest dir).

### Changed
- Renamed the **`emulator` capability → `android`** for clarity now that `apple` is a sibling
  (the `emulator/` package dir — which holds android, apple, and native-mesh helpers — is
  unchanged). `capabilities.has_android()`, `Capabilities.android`, doctor group `android`.
- Bundled `meshtastic-e2e` agent skill (shipped in the wheel) + `skills install` command.
- Hardware-free Android-emulator e2e: `emulator/native_node.py` (UDP-multicast virtual mesh)
  and `emulator/avd.py` (android CLI + adb app-plane driver, incl. `connect_app_to_tcp`).
- MCP tool annotations (readOnly/destructive/openWorld hints + titles).
- Tooling: ruff, mypy (clean, no overrides), hatch-vcs versioning, CI + Trusted-Publishing
  release, `server.json` MCP registry manifest, Dockerfile.
