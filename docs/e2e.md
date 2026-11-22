# End-to-end testing

An e2e test drives an app against a radio and checks both ends. The radio can be simulated or real. The `meshtastic-e2e` skill carries the per-feature recipes; this page is the loop they share.

## Against a simulated radio

`sim` serves a simulated Meshtastic TCP radio on the host. It needs no hardware and no firmware build, and the same seed gives the same mesh every run.

```text
sim(action="start", source="meshcon", port=4403)   # a synthetic mesh
app_view(platform="android", action="screenshot", annotate=True)  # numbered controls
app(platform="android", action="tap", label=12)     # connect the app to 10.0.2.2:4403
inject(session_id=<id>, kind="waypoint", lat=37.77, lon=-122.42, geofence_radius=200)
app_view(platform="android", action="poll_for_text", token="Geofence")  # wait for the app
sim_status(session_id=<id>)                         # check what the app sent back
sim(action="stop", session_id=<id>)
```

- `inject` pushes one packet or FromRadio message into the running session, so a test can trigger exactly the feature it checks.
- `fuzz` on `sim` mixes malformed and hostile packets into the stream to test the app's decoder.
- A source can be a real recorded capture instead of a synthetic mesh, replayed at any speed.

Every `sim` parameter, the synthetic-mesh profile and the fuzz presets are in [replay.md](replay.md). Matching Android and iOS screenshots on one mesh are in [screenshot-set.md](screenshot-set.md).

## Against a real radio

Connect the app to a bench radio, then check the radio's side with the device intents and the recorder:

- `send` and `inspect` act on the radio as the app's peer.
- `wait_for` holds the radio's connection until a matching packet or log line arrives, so a test waits on an event instead of sleeping.
- `query` runs read-only SQL over everything the recorder captured, by time window.

A node never sees its own transmission on the receive path, so a local send never shows in the packet stream. Use `send(..., wait_for_tx=True)` with `security.debug_log_api_enabled` on, or check a second node.

## Injecting frames into real hardware

`inject_frame` delivers a crafted frame into a board's real receive path as if it came off the air. Remote admin, PKI decryption, hop handling and dedup all run, which the phone API cannot reach because it marks every packet as locally sent.

- The board must run firmware built with `-D MESHTASTIC_ENABLE_FRAME_INJECTION=1`. It is off by default and must never ship on, because it forges over-the-air traffic. Portduino `sim` nodes support it always.
- Modes are `text`, `raw`, `admin`, `ciphertext` and `fuzz`. `encrypt=False` sends already-decoded payloads, which the PKI path needs.
- On an nRF52, keep one connection for setup, injection and log capture. Rapid open and close cycles can wedge its USB serial, and `uhubctl_cycle` recovers it.

A standalone CLI for the same frames is `cli/meshinject.py`.
