# Screenshot sets: matching Android and iOS screens

`screenshot_set_start` captures the same screens of Meshtastic-Android and Meshtastic-Apple on one simulated mesh, in light and dark. It registers when the android or the apple capability is active, and lists under the `apple` toolset (see [toolsets.md](toolsets.md)). `screenshot_set.py` holds the run.

## What a run does

1. Serves each app its own replay node from the same seed and clock anchor, so both show the same nodes, channels, last-heard times and messages. A fresh node number each run, shared by both apps, keeps nodes from earlier runs out of the shots.
2. Sets each device's location to the mesh's center.
3. Connects each app through its connect link, which switches the app to the replay node.
4. Plays the capture once, then opens each screen by deep link and screenshots both apps in each appearance.

It returns a `job_id` at once; `screenshot_set_poll` reports status, the log tail and the PNGs captured so far. PNGs land in `<out_dir>/<screen>/<platform>-<appearance>.png`. `out_dir` must be inside `MESHTASTIC_MCP_CAPTURE_DIR` (default: the server's working directory); without it a timestamped directory under the data dir is used. Runs share the emulator and the simulator, so a second run waits for the first.

## Parameters

| Parameter | Meaning |
| --- | --- |
| `screens` | `nodes`, `map`, `messages`, `connections`, `settings`, `settings/<page>` (`about`, `bluetooth`, `device`, `display`, `lora`, `mqtt`, `network`, `position`, `power`, `security`, `serial`, `telemetry`, `user`), `node:<num>`, `channel:<index>`, or `{"name", "android", "ios"}` with explicit deep-link paths. |
| `platforms` | `android`, `ios`; both by default. |
| `appearances` | `light`, `dark`; both by default. |
| `android_serial`, `ios_udid` | Pick the emulator and the simulator; otherwise the first running emulator and the booted simulator (one is booted if none is). |
| `sim_nodes`, `sim_seed` | Size and seed of the synthetic mesh (60 nodes, seed 1337). |
| `settle_s` | Seconds to wait after opening a screen before the shot. |
| `clean_status_bar` | Pins both status bars to 9:41 with full signal and battery, as in App Store shots, and resets them afterwards (default on). |
| `reset_apps` | First returns each app's settings (sort, filters, theme, units) to their defaults, so earlier use cannot show in the shots. Android clears the app's data and needs a debug build, which skips onboarding; iOS deletes the app's UserDefaults and keeps its stored data. |
| `beacon_listen` | Serves both replay nodes with the mesh beacon module listening, as `replay_start(beacon_listen=True)` does. |

## Requirements

A running Android emulator with Meshtastic-Android, and a booted iOS Simulator with Meshtastic-Apple 2.7.28 or later past onboarding, plus idb. A platform that cannot be driven is skipped and named in the log, as is the app each platform used. Afterwards the emulator keeps the mesh's location; the simulator's is cleared.

## Connecting the iOS app by hand

`ios_open_url(port=<port>)` opens the app's connect link for a host-side TCP node, `meshtastic://meshtastic/connections?address=t127.0.0.1:<port>`. With a `replay_start` node on that port, the app shows "Connect to this device?"; confirm it with `ios_tap` or by hand, and the app streams the same simulated mesh the Android emulator gets at `10.0.2.2:<port>`. iOS may first ask "Open in Meshtastic?", which `ios_open_url` confirms when idb is installed (`confirmed_open_sheet`). Connect links need Meshtastic-Apple 2.7.28 or later. `ios_screenshot` then captures the screen, with `appearance` and `clean_status_bar` as above, both of which stay set on the simulator.
