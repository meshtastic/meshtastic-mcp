# ATAK / CoT capture + emulator fleet

Tools to capture the **real shape** of ATAK Cursor-on-Target (CoT) messages and
to drive multiple TAK nodes around a map — the ground truth the
[`TAKPacket-SDK`](https://github.com/meshtastic/TAKPacket-SDK) compresses onto
LoRa. Spec-derived fixtures omit the detail children real ATAK always attaches;
this captures what the app actually emits.

## The pieces

- **`cot_relay_start` / `cot_relay_status` / `cot_relay_stop`** — a plain-TCP CoT
  endpoint (core; pure stdlib). TAK clients connect as an unauthenticated
  streaming input. Every `<event>` is saved to
  `<data_dir>/cot_captures/<session>/NNNN_<type>.xml` + `manifest.jsonl`, and
  rebroadcast to every other connected client (N-way relay).
- **`atak_fleet_up` / `atak_fleet_down`** — clone a base AVD into N provisioned
  ATAK-CIV emulators pointed at the relay (android capability).
- **`atak_drive_route`** — feed an emulator a moving GPS track so its self-PLI
  reports a live course/speed.

## Quick start

```
cot_relay_start(port=8087)                       # bind + start capturing
atak_fleet_up(count=2, apk_path="ATAK-CIV.apk", base_avd="medium_phone")
atak_fleet_status()                              # poll until phase=ready (bring-up is async)
# both nodes now show each other as contacts; PLI/markers/chat flow + capture
atak_drive_route("emulator-5554", [[41.60,-93.77],[41.61,-93.75]], speed_mps=12)
atak_fleet_status()                              # drive progress (driving is async too)
cot_relay_status()                               # peers + per-type event counts
atak_drive_stop("emulator-5554")
cot_relay_stop(); atak_fleet_down()              # add confirm=True to also delete clones
```

`atak_fleet_up` and `atak_drive_route` run in the background (bring-up is ~15 min
cold / ~60 s from snapshot; a drive sleeps per GPS fix) — both return
immediately and you poll `atak_fleet_status`.

A physical ATAK phone can join too: add a streaming input pointed at the host's
LAN IP + port (plain TCP, no SSL/auth).

## Tailing logs (clean, filtered)

Two feeds matter: what the relay captured, and what each device's ATAK is doing.

**Relay event feed** — `manifest.jsonl` is already a clean one-line-per-event
stream (seq, time, peer, type, callsign, bytes):

```bash
tail -F "$(ls -td "${MESHTASTIC_MCP_DATA_DIR:-$HOME/.local/share/meshtastic-mcp}"/cot_captures/*/ | head -1)manifest.jsonl"
# pretty one-liners:  … | jq -rc '"\(.time) \(.peer) \(.callsign) \(.type)"'
```

Or from an MCP session, `cot_relay_status()` — peers now show callsign, not just IP.

**Fleet device logs** — raw `adb logcat` is drowned by the emulator's GL-error
flood. Filter to ATAK's comms tag (`atak.ATAK_LOG_TAG`) so only streaming
connect/retry/rx lines show. One node:

```bash
adb -s emulator-5554 logcat -v time CommsMapComponentCommo:V '*:S'
```

**All fleet nodes at once**, name-prefixed and multiplexed into one stream:

```bash
atak-tail() {  # tail the relay + every emulator's ATAK comms log, prefixed
  local dir; dir="$(ls -td "${MESHTASTIC_MCP_DATA_DIR:-$HOME/.local/share/meshtastic-mcp}"/cot_captures/*/ | head -1)"
  ( tail -F "$dir/manifest.jsonl" | sed 's/^/[relay] /' ) &
  for s in $(adb devices | awk '/^emulator-/{print $1}'); do
    ( adb -s "$s" logcat -v time CommsMapComponentCommo:V '*:S' | sed "s/^/[$s] /" ) &
  done
  wait
}
```

`Ctrl-C` stops it; add `trap 'kill 0' INT` inside for a clean teardown of the
backgrounded tails.

## Learnings baked in (why the tools behave as they do)

What ATAK emits, when, and which types it never emits (so a scenario knows
what must be host-injected) is in [`atak-cot-emission.md`](./atak-cot-emission.md).

- **Emulators are the primary node, not the phone.** `adb emu geo fix` feeds ATAK
  a *genuine* GPS fix. A physical device **rejects Android's mock-location
  provider** for self-position (anti-spoof: `Location.isFromMockProvider()`), so
  its PLI can only be moved by hand. `atak_drive_route` is emulator-only for this
  reason. On the phone, ATAK's manual "set location" tap pins the marker but does
  not clear the "NO GPS" state.
- **`geo fix` takes longitude first.** A silent wrong-hemisphere trap.
  `set_position(lat, lon)` takes map order and swaps internally.
- **GeoChat needs ≥2 relayed clients.** Broadcast GeoChat to "All Chat Rooms" on
  a lone client stays local and never hits the wire — the relay (or a real peer)
  is what makes `b-t-f` chat transmit. This is why the relay rebroadcasts.
- **Prefs go in `config/prefs/defaults` — no extension.** That is the only
  file ATAK ingests (`PreferenceControl.ingestDefaults`, once at
  `ATAKActivity` start, then deleted). A `.pref` in `config/prefs/` or
  `import/` is never read. `provision()` uses it for the stream; the same
  file sets `locationCallsign`, `locationTeam`, `atakRoleType` (e.g. `K9`),
  and `locationReportingStrategy=Constant` (capitalised; `constant` is
  silently ignored and you stay on the 180 s stationary default).
- **Emulators on one host share a LAN** (`10.0.2.x`, ping works), so ATAK
  learns peers' direct `ip:4242:tcp` endpoints and sends GeoChat
  point-to-point — **bypassing the relay**. Set
  `autoDisableMeshSAWhenStreaming=true` (`CommsMapComponent.setPreferStreamEndpoint`)
  so chat + receipts go via the stream and get captured.
- **Nothing but PLI auto-shares.** Markers, shapes, routes and R&B need an
  explicit Send (details pane → Send → Broadcast); only SPI (5 s) and
  "broadcast"-toggled markers repeat. 911 is one-shot per toggle in 5.6.
  `t-x-d-d` is receive-only — inject it. See `atak-cot-emission.md`.
- **Self-PLI speed comes only from the fix.** `drive_route` stamps
  velocity on each `geo fix`; course still reflects the virtual compass
  (no bearing parameter; `geo nmea` is ignored by current emulators).
- **`t-x-c-t` is a real, undocumented type.** ATAK emits a client keepalive ping
  every few seconds; it is **not** in the TAKPacket-SDK fixture corpus.
- **Lone-client reconnect cycle.** A single silent client is dropped by ATAK's
  own link health check about every 2.5 min (it re-sends its self-PLI on
  reconnect, so nothing is lost). With 2+ clients relaying, the link isn't
  silent and this is rarer.
- **First run is scriptable on emulators, manual on the phone.** `provision()`
  walks the EULA → permission rationale → all-files-access → device-setup →
  battery-optimization dialogs. On a physical device the Play Store install and
  EULA are manual.
- **Emulator disk.** ATAK-CIV is ~108 MB; the default 6 GB userdata is ~94% full
  out of the box, so the fleet boots with `-partition-size 8192`.

## Provision-once, restore-fast

The first `atak_fleet_up` cold-boots each clone, installs+provisions ATAK, and
saves a snapshot named `provisioned_<apk-sha>`. Later runs restore that snapshot
(hermetic, `-no-snapshot-save`) — a ~15 min setup becomes a ~60 s bring-up. The
snapshot is keyed on the APK bytes, so a new ATAK build re-provisions instead of
restoring a stale image. `atak_fleet_down(delete_clones=True)` discards the
clones and their snapshots.

## Prompt-injection note

Captured CoT is remote-authored TAK content, attacker-controllable. The full
events (marker/GeoChat bodies included) are written to the capture files on disk;
`cot_relay_status` itself returns only the per-peer **callsign** plus counts. Both
the callsign it returns and the files it writes are untrusted (lethal-trifecta
leg 2) — do not combine with `send_text` in one agentic task without human
review. See `SECURITY.md`.

## `[android-fast]` extra (UI driving)

With `meshtastic-mcp[android-fast]` installed, the existing `android_ui_dump` /
`android_tap` / `android_type_text` MCP tools use a resident **uiautomator2**
server under the hood: ~100× faster hierarchy dumps, dumps that work on animating
screens, and Unicode-safe clipboard-paste input (no `input text` character
mangling — the cause of stray keystrokes when the soft keyboard shifts the
layout). Import-guarded — without the extra every tool falls back to plain adb,
identical behavior otherwise. There is no new MCP tool to call; the fast path is
internal. (In-process code driving the app can use the `avd.tap_text` helper for
an atomic find-then-tap, which re-resolves the element at action time instead of
tapping stale coordinates.)
