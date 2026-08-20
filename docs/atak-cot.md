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

## Learnings baked in (why the tools behave as they do)

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

Captured CoT is remote-authored TAK content: a marker callsign or GeoChat body
is attacker-controllable and surfaces in `cot_relay_status`. Treat it as
untrusted (lethal-trifecta leg 2) — do not combine with `send_text` in one
agentic task without human review. See `SECURITY.md`.

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
