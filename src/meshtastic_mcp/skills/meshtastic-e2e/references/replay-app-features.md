# Replay-driven app testing (features, load, discovery, traceroute, reactions)

For app behavior that reacts to **mesh-delivered packets** — a waypoint geofence raising an
enter/exit notification, a node appearing, a traceroute log filling in, a message-list under
conference-scale chatter — the **replay engine is the ideal device plane**: no radios, no native
mesh, and you control the *exact* packets, pacing, and even how the device advertises itself. This
is lighter and more deterministic than the `meshtasticd` native-node lab.

Five recipes below. All share one session model: `replay_start` returns a `sid`; drive it with
`replay_inject` / `replay_status`; tear down with `replay_stop`. The **CLI** (`meshtastic-mcp
replay …`) serves the same engine standalone (foreground, Ctrl-C to stop) when you don't have an
MCP session — e.g. a human eyeballing the app, or a shell-scripted soak.

---

## A. Feature injection — geofence / waypoints / alerts

```
replay_start(source="meshcon", port=4403)         # a fake radio on the host → sid
# app/AVD connects its TCP device to 10.0.2.2:4403
replay_inject(sid, "waypoint", {...geofence...})   # push the feature's trigger packet(s)
replay_inject(sid, "position", {...}, from_node=…) # a node crossing the boundary
# assert in the app: poll_notification / poll_logcat / ui tree / screenshot
replay_stop(sid)
```

`replay_inject` builds a packet from a high-level `kind`+`args` and emits it onto the live
connection (same send path as the stream). Kinds: `waypoint` (incl. `geofence_radius`, `bbox`
`[south,west,north,east]`, `notify_on_enter/exit/favorites_only`), `position`, `text`, `nodeinfo`,
`beacon`, `traceroute`, `raw`. `fuzz=True` runs the packet through the session's fuzz mutator first
(inject a **deliberately malformed** trigger to test the decoder). For a fully scripted run, build a
`capture.from_events([...])` and `replay_start` that instead.

> The packet **builders** (`replay/build.py`) set proto fields the bundled `meshtastic` package
> predates (e.g. the Waypoint geofence fields) via raw-wire `append_fields` — so you can test a
> feature whose proto is newer than the installed Python lib.

Worked example (geofence): inject a `waypoint` with `geofence_radius=500, notify_on_enter/exit`,
then three `position` packets from a tracker node — **outside → inside → outside** — and assert
`poll_notification("entered")` then `poll_notification("left")`. Validated end-to-end against
Meshtastic-Android #6014.

---

## B. Sustained-load / stress replay

Drive the app under conference-scale traffic to shake out ANRs, message-list jank, node-DB churn,
and dropped-packet handling. The **`conference-stress` preset** is the batteries-included scenario:
the DEF CON geometry/channels/RF model at *gateway-observed* density (per-node cadences throttled
so 1600 nodes ≈ a real capture's ~18–20k packets, not the omniscient firehose) with the BBS/bot
plane on (recipe E).

```
replay_start(source="conference-stress", sim_nodes=1600, rate=140, loop=True)   # MCP
```
```bash
meshtastic-mcp replay conference-stress --nodes 1600 --rate 140 --loop          # shell
```

**Pacing** (precedence, highest first):
- `duration=N` — stream the whole windowed capture in exactly N wall-clock seconds, whatever its
  packet count (`duration=150` = the whole capture in 2.5 min). The derived rate is `packets/N`.
- `rate=R` — steady R packets/sec, ignores capture timing.
- `speed=X` — multiply the capture's original cadence (`max_gap` caps idle stretches).

Pacing is **drift-free** (deadline-anchored): a requested rate is actually delivered, not ~80% of
it. **Self-verify** with `replay_status` — it reports `target_rate` vs the live `achieved_rate`
(re-anchored per connection, so an idle gap between app reconnects doesn't dilute the reading):

```
st = replay_status(sid)
# assert abs(st["achieved_rate"] - st["target_rate"]) / st["target_rate"] < 0.05
```

**What to assert (app side):** the app stays responsive under load — the a11y tree still answers
(`poll_for_text`), no ANR dialog in `poll_notification`/`poll_logcat`, the node list reaches the
expected count, the message list scrolls. Pair with the recorder’s `logs_window` on the app’s
logcat for the same window if you ship logs there.

**Local device metrics:** the connected node reports its own `DEVICE_METRICS` telemetry every
`local_metrics_interval` seconds (default 300; `0` to disable) — battery/voltage/uptime plus a
channel-utilization that tracks the live replay rate. So the app's **Device Metrics** view for the
device it's connected to fills in and updates: `poll_for_text` an uptime/battery/util value, or
assert the util reads "busy" under a high-rate stress stream. `replay_status().local_metrics`
reports the interval + emitted count.

**Disconnect-survival is now guaranteed:** with `loop=True`, closing/backgrounding the app (or a
`Connection reset by peer`) severs only *that* connection — the session keeps listening and the
next reconnect handshakes and streams a fresh pass. So a soak test can bounce the app repeatedly
against one long-lived session (`packets_sent` keeps climbing across reconnects).

---

## C. Bonjour / mDNS auto-discovery

A session advertises itself over mDNS/Bonjour as `_meshtastic._tcp` on `local.` with the
`shortname`/`id` TXT records real firmware publishes — so the app lists it in **network discovery**
with no manual IP entry. Auto-on for non-loopback binds; `mdns=False` to opt out.

```
st = replay_start(source="conference-stress", sim_nodes=1600, rate=140)
st["mdns"]   # {"advertised": true, "backend": "dns-sd", "display_name": "RPLY_4331", ...}
```

**Assert (app side):** open the app’s Add-Device / network picker and `poll_for_text("RPLY_")` —
the session shows as `RPLY_<last 4 of id>` (e.g. `RPLY_4331`). Tapping it connects without typing an
IP. **Verify from the shell** it’s on the wire:

```bash
dns-sd -B _meshtastic._tcp local.                       # macOS: session appears in the browse list
dns-sd -L "Meshtastic Replay <label>" _meshtastic._tcp  # resolves host:port + shortname/id TXT
avahi-browse -rt _meshtastic._tcp                        # Linux equivalent
```

Backends are best-effort in preference order: the `zeroconf` package if installed, `dns-sd`
(macOS), `avahi-publish-service` (Linux). None present → `st["mdns"]["advertised"]` is `false` with
an actionable `error` hint; the **session still runs**, you just connect by IP.

---

## D. Traceroute log

Apps surface only traceroute **responses** in their traceroute log (Settings → Traceroute Log on
Apple): the handler ignores in-flight *requests* (`decoded.request_id == 0`) and persists only
responses (nonzero `request_id`). The sim emits **request → response pairs** with firmware
`RouteDiscovery` semantics (relays-only route, endpoints implied by from/to, `len(route)+1` SNR
entries, `hop_start > 0`), so the log fills in on its own during any `conference-stress`/`defcon`
replay.

- **Passive:** stream `conference-stress` (or any `defcon` sim with the bot plane — attendees trace
  the bots) and assert the traceroute log grows: `poll_for_text` a known hop-list row, or navigate
  Settings → Traceroute Log and read the entry count off the tree.
- **App-initiated:** issue a traceroute from the app toward a synthetic node; the engine’s live
  responder answers with a well-formed `RouteDiscovery` addressed back to you, echoing your
  `request_id`, so the outgoing traceroute resolves in-UI (hop list + SNR + `route back`).

> Craft a response by hand with `replay_inject(sid, "traceroute", {route:[…relays], snr_towards:[…],
> route_back:[…], snr_back:[…], request_id: <target>}, from_node=<dest>, to_node=<requester>)`.
> `request_id` must be nonzero or the app treats it as a request and drops it.

---

## E. Reactions (tapbacks) + the BBS/bot plane

Tapbacks are `TEXT_MESSAGE_APP` packets with `decoded.emoji=1` and `decoded.reply_id` = the reacted
message’s packet id. The **bot plane** (`sim_profile={"bots": {"count": N, …}}`, on by default in
`conference-stress`) generates the realistic scene: 17 meshing-around-style auto-reply bots
(ping→pong pile-ons, `cmd`/`motd`/`wx`/`joke`/games menus) egged on by attendees, plus tapback
storms threading real packet ids — a power-law long tail **and one legendary broadcast collecting a
big reaction storm** (147 in `conference-stress`; tune with `tapback_storm`). That message’s body
contains the literal word **`tapback`**, so it’s findable:

```
replay_start(source="conference-stress", sim_nodes=1600, rate=140, loop=True)
# app: search messages for "tapback" → the legendary broadcast; open it →
poll_for_text("147")   # reaction count on the legendary message
```

**Assert (app side):** the reaction UI renders — a reaction count/badge on the legendary message, or
the message list stays smooth while thousands of reactions arrive (load angle). Tune volume with
`sim_profile={"bots": {"count": 17, "tapback_storm": 150, "storms_per_day": 200}}`.

**Targeted single reaction:** `replay_inject(sid, "text", {"body": "👍", "reply_id": <target_id>,
"emoji": True}, from_node=…)`. The catch: `reply_id` must be a packet id the app has actually
received. Inject doesn’t return the ids it mints, so for a deterministic target either react within
a scripted `from_events` capture (you control the ids), or lean on the bot plane (which threads ids
internally) rather than hand-injecting the pair.

---

## Oracles (assert the app reacted)

- **`poll_notification(token, timeout)`** — `dumpsys notification` for features that surface as
  system notifications (geofence enter/exit, message alerts, ANR). Returns the matching line.
- **`poll_logcat(token, timeout, tags=…)`** — logcat for log-visible app events (dispatch,
  workers, lifecycle). Call **`clear_logcat()` before the stimulus** so a prior run can't
  false-positive.
- **`poll_for_text(token)`** — the a11y-tree oracle (visible UI text): device rows, hop lists,
  reaction counts, node counts.
- **vision oracle** (`vision-oracle.md`) — screenshot + VLM when the tree is empty (map overlays,
  Canvas message list).

---

## Gotchas (cost real time when missed)

- **The replay DUT must outlive a single tool call.** Replay sessions live in the process that
  started them — drive replay via the **persistent MCP server** (`replay_start`/`replay_inject`/
  `replay_stop` across calls), or the **`meshtastic-mcp replay` CLI** for a standalone foreground
  server. Shell-*backgrounded* scripts get reaped between commands; if you must script standalone,
  run replay + drive + assert in **one** process, or use the CLI (it blocks until Ctrl-C).
- **A leftover replay on the port silently hijacks the app.** If port 4403 is already taken, the
  app connects to *that* server (you'll see the wrong nodes). `replay_start` raises a clear
  port-in-use error (or pass `port=0` to auto-pick). Check `replay_status().connect` for the real
  address; `replay_stop()` (no id) stops **all** sessions.
- **Two `_meshtastic._tcp` advertisers look identical in the picker.** A real WiFi-mDNS node on the
  LAN and your replay session both show up; the replay row is `RPLY_<id>`. Match on that, or
  `--no-mdns` / `mdns=False` and connect by IP to be unambiguous.
- **A requested rate that isn’t met means the machine is the bottleneck,** not the pacer — the pacer
  is drift-free up to the raw send ceiling (thousands/sec). If `achieved_rate` sits below
  `target_rate` at a modest rate, look at the *reader*: a backgrounded/slow app stops draining its
  socket and back-pressures the send (bounded by `send_timeout`), which the status will show as a
  disconnect, not a slow stream.
- **Map-overlay visuals need the F-Droid (OSMDroid) flavor.** Google Maps tiles don't render in the
  emulator without an API key/network — the map is blank, so circle/box overlays and traceroute map
  flyovers aren't visible. Build/install the `assembleFdroidDebug` variant for offline-tile map
  assertions; the alert-engine/traceroute logic itself is flavor-independent (assert via
  notifications / the traceroute log list).
- **Use the `universal` debug APK on x86_64 emulators** when a build produces no x86_64 split (e.g.
  `assembleGoogleDebug` may emit only arm + universal). The universal APK carries x86_64.
- **`pkill -f <pattern>` self-matches** — `pkill -f replay` kills the very command running it (its
  cmdline contains the pattern). Kill by PID, or exclude the current shell.
- **Clear app state for a clean run** (`adb shell pm clear <pkg>`) — the app persists its node DB
  across sessions, so stale nodes from a prior replay linger. Re-grant runtime permissions after.
