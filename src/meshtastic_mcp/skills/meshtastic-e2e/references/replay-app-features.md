# Replay-driven app-feature testing (geofence, waypoints, alerts…)

For app features that react to **mesh-delivered packets** (a waypoint geofence raising an
enter/exit notification, a waypoint rendering on the map, a node appearing, a DM ack), the
**replay engine is the ideal device plane** — no radios, no native mesh, and you control the
*exact* packets. This is lighter and more deterministic than the `meshtasticd` native-node lab.

## The loop

```
replay_start(source="meshcon", port=4403)        # a fake radio on the host
# app/AVD connects its TCP device to 10.0.2.2:4403
replay_inject(sid, "waypoint", {...geofence...})  # push the feature's trigger packet(s)
replay_inject(sid, "position", {...}, from_node=…) # a node crossing the boundary
# assert in the app: poll_notification / poll_logcat / ui tree / screenshot
replay_stop(sid)
```

`replay_inject` builds the packet from a high-level `kind`+`args` and emits it onto the live
connection (same send path as the stream). Kinds: `waypoint` (incl. `geofence_radius`, `bbox`
`[south,west,north,east]`, `notify_on_enter/exit/favorites_only`), `position`, `text`,
`nodeinfo`, `raw`. `fuzz=True` runs the packet through the session's fuzz mutator first (inject a
**deliberately malformed** trigger to test the decoder). For a fully scripted run, build a
`capture.from_events([...])` and `replay_start` that instead.

> The packet **builders** (`replay/build.py`) set proto fields the bundled `meshtastic` package
> predates (e.g. the Waypoint geofence fields) via raw-wire `append_fields` — so you can test a
> feature whose proto is newer than the installed Python lib.

## Oracles (assert the app reacted)

- **`poll_notification(token, timeout)`** — `dumpsys notification` for features that surface as
  system notifications (geofence enter/exit, message alerts). Returns the matching line.
- **`poll_logcat(token, timeout, tags=…)`** — logcat for log-visible app events (dispatch,
  workers, lifecycle). Call **`clear_logcat()` before the stimulus** so a prior run can't
  false-positive.
- **`poll_for_text(token)`** — the a11y-tree oracle (visible UI text).
- **vision oracle** (`vision-oracle.md`) — screenshot + VLM when the tree is empty.

Worked example (geofence): inject a `waypoint` with `geofence_radius=500, notify_on_enter/exit`,
then three `position` packets from a tracker node — **outside → inside → outside** — and assert
`poll_notification("entered")` then `poll_notification("left")`. Validated end-to-end against
Meshtastic-Android #6014.

## Gotchas (cost real time when missed)

- **The replay DUT must outlive a single tool call.** Replay sessions live in the process that
  started them — drive replay via the **persistent MCP server** (`replay_start`/`replay_inject`/
  `replay_stop` across calls), not ad-hoc backgrounded scripts. Shell-backgrounded servers get
  reaped between commands; if you must script standalone, run replay + drive + assert in **one**
  process.
- **A leftover replay on the port silently hijacks the app.** If port 4403 is already taken, the
  app connects to *that* server (you'll see the wrong nodes). `replay_start` now raises a clear
  port-in-use error (or pass `port=0` to auto-pick). Check `replay_status().connect` for the real
  address; `replay_stop()` (no id) stops **all** sessions.
- **Map-overlay visuals need the F-Droid (OSMDroid) flavor.** Google Maps tiles don't render in
  the emulator without an API key/network — the map is blank, so circle/box overlays aren't
  visible. Build/install the `assembleFdroidDebug` variant for offline-tile map assertions; the
  alert-engine logic itself is flavor-independent (assert via notifications).
- **Use the `universal` debug APK on x86_64 emulators** when a build produces no x86_64 split
  (e.g. `assembleGoogleDebug` may emit only arm + universal). The universal APK carries x86_64.
- **`pkill -f <pattern>` self-matches** — `pkill -f geofence` kills the very command running it
  (its cmdline contains the pattern). Kill by PID, or exclude the current shell.
- **Clear app state for a clean run** (`adb shell pm clear <pkg>`) — the app persists its node DB
  across sessions, so stale nodes from a prior replay linger. Re-grant runtime permissions after.
