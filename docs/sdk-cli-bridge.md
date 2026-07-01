<!--
SPDX-FileCopyrightText: Meshtastic contributors
SPDX-License-Identifier: GPL-3.0-only
-->
# Kotlin-SDK device-IO bridge (experimental)

An optional capability that drives a device through the **Meshtastic Kotlin SDK**
([`meshtastic/meshtastic-sdk`](https://github.com/meshtastic/meshtastic-sdk)) instead of the
Python `meshtastic` library — by shelling out to that project's headless JVM sample CLI, exactly
the way the MCP already shells out to `pio` / `adb` / `idb` / `esptool`. It exists to evaluate the
SDK's engine (BLE / TCP / USB-serial, two-stage handshake, NodeDB, ACK correlation) as an
alternative device backend.

The tools register only when the CLI launcher is resolvable (`capabilities.has_sdk_cli()`), so a
plain install is unaffected.

## Setup

Build the SDK CLI once (Gradle `application` plugin → `installDist`):

```
git clone https://github.com/meshtastic/meshtastic-sdk
cd meshtastic-sdk && ./gradlew :samples:cli:installDist
```

Then point the MCP at it, either way:

- `MESHTASTIC_MCP_SDK_CLI` — absolute path to the `cli` launcher
  (`samples/cli/build/install/cli/bin/cli`).
- `MESHTASTIC_SDK_ROOT` — a meshtastic-sdk checkout; the launcher is derived from it.

## Tools

- **`sdk_status`** — whether the CLI resolves, and from which env var.
- **`sdk_device_info(transport, timeout_ms)`** — one-shot snapshot (own node + node count).
- **`sdk_list_nodes(transport, timeout_ms)`** — node-DB snapshot (each node's wire-JSON).
- **`sdk_send_text(transport, message, to, channel, await_ms, timeout_ms)`** — transmit and await
  Acked/Delivered/Failed (device-mutating).

`transport` accepts the SDK syntax `tcp:host[:port]` / `serial:port[:baud]` / `ble:needle`, and
also `tcp://host` or a bare serial device path for convenience. A separate JVM process owns the
radio link; the bridge only builds argv, runs it, and parses stdout.

## Wire contract

The CLI's `--json` mode emits NDJSON — one `{"type","ts","data"}` envelope per line — terminated by
a `done` (`data:{reason,exit}`) or `error` (`data:{code,message}`) envelope. The bridge parses the
stream into `{ok, exit, by_type, envelopes, error, stderr}`; a device-level failure surfaces as
`ok=false` with `error`, not an exception.

## Status

Validated end-to-end against the **real** CLI binary (not just a fake launcher):

- The envelope parser matches the live wire format — a TCP connect to a dead port yields the exact
  `error` + `done` envelopes the bridge expects (`ok=false`, parsed `error.code/message`).
- **Cross-validation against the MCP's own replay simulator** (Kotlin SDK ↔ Python MCP): the SDK
  engine connects over TCP and successfully drains **Stage 1 (config) and Stage 2 (full NodeDB)**
  from the MCP's synthetic mesh — i.e. the MCP's simulated PhoneAPI stream is correct enough to
  drive the real SDK engine.

  Originally the SDK then timed out at its final **"Seeding Session"** step, because that step
  issues a `get_owner_request` admin round-trip and the replay engine only answered
  `want_config_id` (a one-way packet streamer, not an admin responder). **That gap is now closed**
  — the replay engine answers `get_owner_request` with the owner + a `session_passkey` (see
  `replay/engine.py`), so a strict client reaches its ready/seeded state. Re-running the SDK CLI
  against the replay sim through that step is the remaining live check to confirm end-to-end.

Building the CLI required two local `build-logic` patches for Kotlin-2.4.0 drift on `main` (the
removed `AbiValidationMultiplatformExtension` DSL; SKIE not yet supporting 2.4.0) — both
iOS/publish-only concerns, irrelevant to the JVM CLI, but a concrete pre-1.0 churn data point.

Treat the CLI shell-out itself as a proof-of-concept; the durable path is the SDK's roadmapped
`host-rpc-server` (a long-lived Ktor WebSocket sidecar the MCP would speak to over a versioned
envelope).
