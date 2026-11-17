# Replay: a simulated Meshtastic device

`replay_start` serves a capture as a radio over TCP, the inverse of the recorder. An app, an Android emulator at `10.0.2.2:<port>`, or the meshtastic Python library connects to `host:port`, does the want-config handshake, and receives a paced stream of packets restamped to "now", as from a real radio. Each session runs in a background thread. The call returns the session status (id, listen address, totals); `replay_status` polls it and `replay_stop` tears it down. `meshtastic-mcp replay` serves the same from a shell.

## Sources

| `source` | What it serves | `kind` |
| --- | --- | --- |
| `meshcon` or `sim` | A generated synthetic MeshCon mesh, no file. | `sim` |
| `burningman`, `defcon` | A generated, calibrated event scenario: playa or convention geography, channels, the RF observer model, an encrypted and MQTT mix, and scripted traffic spikes. | `sim` |
| `conference-stress` | The dense-convention stress scenario. | `sim` |
| `*.db`, `*.db.gz` | A SQLite capture in the Burning Man / DEF CON / MeshCon schema, full-fidelity payloads. | `sqlite` |
| `*.jsonl` | A recorder `packets.jsonl`, best effort, payloads truncated. | `jsonl` |

`kind="auto"` (the default) infers the kind from the source string.

## Pacing and window

Highest priority first:

1. `duration` compresses the whole windowed capture into that many wall-clock seconds, a steady `packets / duration`, so `duration=150` replays all of a capture in 2.5 minutes whatever its size.
2. `rate` sends a steady number of packets per second and ignores the capture's timing.
3. `speed` multiplies the original cadence, with idle gaps capped at `max_gap` seconds.

Pacing is deadline-anchored, so drift does not accumulate and a requested rate is delivered. `replay_status` reports `target_rate` against the live `achieved_rate`, so a stress run checks itself.

`start` and `end` (ISO-8601, UTC when no zone is given) window the capture. `loop` restarts at the end. `limit_nodes` caps the node DB of a file source.

## Synthetic mesh

`sim_nodes`, `sim_days` and `sim_seed` size and seed the generator. Without `start`, a synthetic capture ends now, so a fresh app sees recent data.

`sim_profile` tunes a sim or preset source further: a dict, or a JSON-object string, of overrides deep-merged over the preset that `source` names. It is never a file path. An invalid profile (bad JSON, not an object, an unknown `beacon_presets` name) fails the call with the cause. It turns on and shapes features the presets leave off, for example:

| Override | Effect |
| --- | --- |
| `{"tak": {"team_nodes": 6, "wire": "v2"}}` | An ATAK squad; `wire="v2"` needs the tak capability. |
| `{"spikes": [{"start_h": 20, "hours": 2, "text_x": 8}]}` | A scripted traffic spike. |
| `{"observer": {"enabled": true, "loss_floor": 0.5, "mqtt_fraction": 0.3}}` | The RF gateway observer model. |
| `{"bots": {"count": 17, "storms_per_day": 200, "tapback_storm": 150}}` | A BBS and bot plane: auto-reply bots (ping-to-pong pile-ons, menu commands) egged on by attendees, tapback (emoji reaction) storms including one legendary message with 100+ reactions, bot beacons, and attendee-to-bot traceroute pairs. |

The RF gateway observer set by `sim_profile["observer"]` is not the connected node; `observer_lat`/`observer_lon` below place that.

## Channels

`channels` (SQLite sources) is a caller-supplied channel list. It routes packets by their OTA channel hash and advertises the real PSKs, so the app live-decrypts encrypted packets. Each entry is `{"name": str, "psk": "<base64>", "primary": bool, "ota_hashes": [int, ...], "catch_all": bool}`. `psk`, `ota_hashes` and `catch_all` are optional: a channel's hash is derived from its name and PSK when not given, and a `catch_all` channel receives packets that match no hash. Without `channels`, the capture's name column becomes the channels, with placeholder keys.

## The connected node

| Parameter | Effect |
| --- | --- |
| `announce_interval` | Above 0, adds a "Replay Clock" node that posts a kickoff and a periodic "ETA - done/total" progress message to the busiest channel, so the app shows it is a replay. |
| `stats_interval` | How often the connected node publishes LocalStats while replay packets flow. 0 stops the periodic updates but keeps the initial snapshot before the node DB and, for a replay that does not loop, the final one. |
| `dupe_every` | Simulates a duplicate radio receive every Nth replay packet and counts it in LocalStats. 0 disables it. |
| `local_metrics_interval` | Emits a `DEVICE_METRICS` telemetry packet from the connected node every N seconds (default 300, 0 = off), as firmware reports its own battery, voltage, channel utilization, air utilization and uptime, so the app's Device Metrics view fills in. Channel utilization tracks the live replay load. |
| `modem_preset` | The advertised LoRa preset, such as `LONG_FAST` or `SHORT_TURBO`. |
| `firmware_edition` | The app's event banner: `VANILLA`, `DEFCON`, `BURNING_MAN`, `HAMVENTION`, and others. |
| `firmware_version` | Overrides the reported build. Unset, an event edition reports that event's real build, so the app's event-info build comparison behaves as with a live event node; an edition with no published build (see `EVENT_FIRMWARE_VERSIONS` in `replay/engine.py`) reports the historical placeholder. |
| `observer_lat`, `observer_lon` | Place the connected node, the app's "you are here", in 1e-7 degrees. The default is the capture's median position, so the map and node distances look right. |
| `observer_num` | This session's node number: identity, device id and mDNS TXT record. Unset, it is the process-wide default (`MESHTASTIC_REPLAY_OBSERVER_NUM`). Pass a distinct value per call to run concurrent sessions that do not present the same identity. |

## One mesh in several apps

- `clock_anchor` (epoch seconds) stamps the window's last packet at that instant and keeps the capture's own spacing, instead of stamping each packet "now" as it is sent. Sessions given the same anchor show their apps the same last-heard and message times.
- `hold` plays the window once per connection, then stays connected and quiet, and takes a client back after it hangs up, as a radio does.
- `beacon_listen` advertises the mesh beacon module as listening, so the app keeps the `MESH_BEACON_APP` packets it would otherwise drop.

`screenshot_set_start` uses all three to show the Android and iOS apps one mesh; see [screenshot-set.md](screenshot-set.md).

## Discovery

`mdns` advertises the session over mDNS/Bonjour as `_meshtastic._tcp` with the `shortname` and `id` TXT records real firmware publishes, so apps list it in network discovery (as `RPLY_4331`) with no manual IP entry. The default `None` is on unless the session binds loopback only. It is best effort: it prefers the `zeroconf` package, falls back to `dns-sd` or `avahi-publish-service`, and otherwise reports a hint under `mdns` in `replay_status`.

## Fuzzing

`fuzz` turns the stream hostile with fault injection and bad actors. It takes a preset name or a dict of overrides, optionally with a `preset` base, such as `{"preset": "adversary", "flooder_rate": 20}`.

| Preset | Faults |
| --- | --- |
| `light` | Rare corruption and loss. |
| `parser` | Malformed bodies and bad values that exercise the decoder. |
| `adversary` | Spoofing, flooding, forged ACKs, rogue ADMIN, evil-twin impersonation. |
| `chaos` | Everything, frame corruption included. |

`fuzz_seed` seeds it so a crash reproduces. `replay_status` reports fuzz activity (counts and recent events) under `fuzz`. `replay_fuzz_presets` lists the presets with the fault categories each enables.

## Injecting packets into a running session

The inject tools emit packets on a live session's connection, through the same send path as the stream, to drive app features on demand. They are the targeted counterpart to the fuzzer's random campaigns. Unless `from_node` is given, the sender is a synthetic injector node.

`replay_inject(session_id, kind, args)` builds a packet from a high-level `kind`:

| `kind` | `args` |
| --- | --- |
| `waypoint` | `lat`, `lon`, `name`, `geofence_radius` (m), `bbox` `[south, west, north, east]`, `notify_on_enter`, `notify_on_exit`, `notify_favorites_only`, `icon` |
| `position` | `lat`, `lon`, `altitude` |
| `text` | `body` |
| `nodeinfo` | `id`, `long_name`, `short_name`, `hw_model`, `role` |
| `raw` | `portnum`, `payload_hex` |

`fuzz=True` runs each packet through the session's active fuzz mutator first, to inject a deliberately malformed packet. `count` repeats the inject.

A geofence test: start a session, then

```python
replay_inject(
    sid,
    "waypoint",
    {
        "lat": 37.0,
        "lon": -122.0,
        "geofence_radius": 500,
        "notify_on_enter": True,
        "notify_on_exit": True,
        "name": "Test",
    },
)
replay_inject(sid, "position", {"lat": 37.0, "lon": -122.0}, from_node=tracker)
```

The wrappers below build one packet type with typed arguments:

- `replay_inject_waypoint` sends `WAYPOINT_APP` (portnum 8) with full geofence support: `geofence_radius` (a circle in meters), `bbox` (the waypoint's `bounding_box`, `[south, west, north, east]` in decimal degrees), either or both, plus `notify_on_enter`, `notify_on_exit` and `notify_favorites_only`, so an app's enter and exit alerts (the Apple app's, for one) can be exercised without hardware. Follow it with `replay_inject(sid, "position", {"lat": ..., "lon": ...}, from_node=tracker)` to move a synthetic node across the boundary.
- `replay_inject_beacon` sends `MESH_BEACON_APP` (portnum 37), the same packet as `replay_inject(sid, "beacon", ...)`, which lets an app (the Apple app among others) exercise Local Mesh Discovery (capturing beacons, adding a beacon's preset or channel to a scan, offering "switch to this channel") without beaconing hardware. `message` is the beacon text (at most 100 bytes on real firmware); `offer_channel_name` and `offer_channel_psk_hex` advertise a channel the app can offer to switch to; `offer_region` and `offer_preset` advertise the beaconed mesh's LoRa region and preset.
- `replay_inject_traceroute` sends a `TRACEROUTE_APP` RouteDiscovery reply, to test the traceroute UI (hop list, SNR coloring, map flyover). `destination_node` is the node the reply comes from. `route` lists the node numbers along the path, destination last; without it a synthetic origin, relay and destination path is used so the UI has hops to render. `snr_towards` and `snr_back` are per-hop SNR values; omit them and the reply carries none, so pass them to test SNR coloring. The engine also answers a connected client's live traceroute requests on its own; this tool pushes an unsolicited reply.
- `replay_inject_fileinfo` sends a raw `FileInfo` FromRadio message. It is a handshake-only FromRadio variant (`STATE_SEND_FILEMANIFEST`) with no MeshPacket envelope, which real firmware sends only during the config handshake. Most app handlers do not gate on handshake state, so this exercises that path in a long-running session: many entries with `count` (each gets a distinct `file_name` suffix, to probe whether a client caps its manifest), or malformed ones (a huge `file_name`, a negative `size_bytes`).
- `replay_inject_client_notification` sends a `ClientNotification`, a top-level FromRadio with no MeshPacket envelope that firmware pushes for events the client must surface. A healthy device cannot produce them on demand, so this drives the app's notification UI. `variant` is `low_entropy_key` (the default: the pre-2.8 weak-key detection, "Compromised keys were detected and regenerated.", which pairs with the firmware fix that rejects a restored weak key), `duplicated_public_key` (another node advertises a key that duplicates a known one), `key_verification_number_request`, `key_verification_number_inform` or `key_verification_final` (the three steps of contact key verification; `nonce`, `remote_longname`, `security_number`, `verification_characters` and `is_sender` fill their fields), or `text` (only `message` and `level`). `message` overrides the canned text, which follows firmware's wording; `level` is `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. `count` repeats it.
