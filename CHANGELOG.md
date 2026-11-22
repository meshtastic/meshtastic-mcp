# Changelog

All notable changes are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com); versions follow SemVer.

## [Unreleased]

### Added
- **More requests from `send`** - `request` takes `position`, `neighbor_info` and `node_info`
  besides `traceroute` and `telemetry`, and `telemetry` names the metrics it asks for (`device`,
  `environment`, `air_quality`, `power`, `local_stats`, `health`, `host`). Every request goes with
  want_ack and the radio's own `lora.hop_limit`, which firmware needs before it answers.
- **Remote admin** - `admin` and `configure` take `dest` to act on another node over PKI admin.
  A node that does not list this radio's key in `security.admin_key` is named as the reason.
- **`configure` keys beyond the config sections** - `owner.is_licensed` and
  `owner.is_unmessagable`, `channel.N.*` to edit or add a channel and `channel.N.delete`,
  `fixed_position` (set, or null to clear), `canned_messages` and `ringtone`.
- **`admin` actions `set_time` and `add_contact`**, `inspect(what="contact_url")` for this radio's
  shareable contact, and `set_channel_url(add_only=True)` to add channels without replacing the set.
- **`doctor` checks the server itself** - a running `serve` on other code than this install, a
  git or PyPI install next to a workspace checkout at another commit, and a binary that exists
  but is off the server's PATH (systemd's default PATH has no `sbin`) are `degraded`, each with
  the fix. A `serve` running from a checkout records its commit, so `doctor` flags one started
  before the last pull, and the server names the commit it runs in its startup line.
- **`nrf52_mount_uf2` and `nrf52_eject_uf2`** - mount a board's UF2 drive on a host that does not
  automount it, and eject it the way a desktop does, through udisks2 or `diskutil`, for bootloader
  benches; flashing never needs them. A polkit refusal over SSH prints the one-shot rule, and
  `doctor` reports it on Linux.
- **A hidden or removed tool name says what replaces it** - a call to `device_info` while
  `legacy` is hidden names `inspect` and the `all,legacy` opt-in instead of `Unknown tool`.

- **Generated configuration and index.** `docs/configuration.md` lists every environment variable
  the package reads, from `env_vars.py`; a test fails when the code reads one it does not name.
  The group table in `docs/toolsets.md` and `llms.txt` are generated too. New pages:
  `docs/bench.md` for a shared bench and `docs/e2e.md` for the app test loop.

### Changed
- **Irreversible tools ask a person by default** - factory resets, erases, `vanity_apply` and
  destructive `vendor` calls no longer run on the agent's own `confirm=True`; a client that cannot
  ask gets a `Stopped:` refusal. `MESHTASTIC_MCP_CONFIRM` defaults to `human`, and `agent` restores
  the old behavior for an unattended bench.
- **One device path** - `set_channel_url`, `send_input_event`, the `meshtastic://device/*`
  resources, `nrf52_enter_uf2`, `install`'s UF2 entry, the `info` and `nodes` CLI commands and the
  legacy tools all reach the radio through the backend the intent tools use, and so do the web
  control plane, recovery, the PA sweep, the RF oracle and the hardware suite. `info.py` and
  `admin.py` are gone. Legacy `get_config` now withholds the fields the protos mark sensitive,
  as `inspect` does; config snapshots still keep them for restore. `meshtastic>=2.7.10` is
  required, for its fixed `factoryReset` and its contact URLs.
- **The pre-1.0 tools are opt-in** - `device_info`, `get_config`, `set_config`, `send_text`,
  `reboot`, the `replay_*` tools and the rest form a `legacy` toolset that is hidden unless named:
  `MESHTASTIC_MCP_TOOLSETS=all,legacy`. `inspect`, `configure`, `send`, `admin`, `sim` and
  `inject` replace them, and `legacy` is removed in 1.1.
- **The default tool list is about 40 tools** - the tools a 1.0 tool replaces joined `legacy`:
  the `_start`/`_poll` pairs (`job` follows every job), the recorder windows,
  `telemetry_timeline` and `triage_bundle` (`query`), `serial_read` and `serial_list`,
  `list_devices` (`devices(include_unknown=True)`), the per-flasher tools (`install`, `vendor`),
  the `android_*`/`ios_*` drivers `app` and `app_view` cover, `replay_fuzz_presets`
  (`sim_status(fuzz_presets=True)`) and the `sdk_*` bridge (`MESHTASTIC_MCP_BACKEND=sdk`).
  Unset `MESHTASTIC_MCP_TOOLSETS` now exposes `core`, `app`, `hardware` and `replay`; `all`
  adds the specialist groups `firmware`, `display`, `android`, `apple`, `tak`, `rf` and `sdk`.
  Calling a tool from a group left out says which setting adds it.
- **`job`, `install` and `vendor` return typed results** - their `outputSchema` names the
  fields, like the other 1.0 tools.
- **Each tool declares its own hints** - `@meta(...)` above a tool's `def` sets its
  read-only, destructive, idempotent and open-world hints, its title and what its `port`
  means for leases, replacing the name lists in `server.py`. `server` no longer re-exports
  the tools and helpers; import them from the module that defines them.
- **Discord moved to its own server** - the `discord_*` tools are served by
  `meshtastic-mcp-discord`, not this server.
- **A stdio client on the bench host bridges to a running `serve`** - so one server owns the
  radios. `MESHTASTIC_MCP_STANDALONE=1` runs a separate server anyway.
- **`duckdb` is a core dependency** - for the recorder's `query` and `wait_for`.
- **Every tool call leaves a server-log line** - on stderr, with the tool, its target radio, the
  duration and, on failure, the reason. Argument values other than the target are never logged.

### Removed

- `docs/sim-realism-plan.md`, a plan rather than a user doc.
- **The local-model tools** - `summarize_window`, `vision_oracle`, `triage_window` and the
  `local_model_*` tools.

### Fixed
- **Snapshots keep owner and channels** - `config_snapshot` and the snapshot `configure` takes
  before a write now hold the owner and all eight channel slots, keys included, and
  `configure(restore=...)` puts them back. A restore after an erase left the radio unnamed and on
  default channels. Channel keys are withheld in `config_diff`. Older snapshots still restore.
- `nrf52_flash_uf2` flashes a bootloader self-update (`update-*.uf2`), which it refused as built
  for the other SoftDevice, and asks even with `confirm=True` under `MESHTASTIC_MCP_CONFIRM=human`.
- A refusal from meshtastic-python (an unknown destination name, a blank owner name, a config
  section it cannot write) is a tool error, not the end of the server.
- Waiting for a restarted radio survives the library's handshake timeout, and a `configure` whose
  read-back fails still returns its snapshot.
- `admin` resets wait out the firmware's restart timer before reconnecting.
- `install` writes a `.bin` at 0x0 only when it is a `firmware-*.factory.bin` image.
- `MESHTASTIC_MCP_TOOLSETS` naming `discord` or `local-model` logs where they went instead of
  stopping the server.
- `config_diff` shows that a key or password changed, not its value, unless `reveal_secrets`.
- A flash or vendor call that names no port is refused while another session holds a lease.

## [0.4.0] - 2026-11-17

### Added
- **Typed results and parameter docs** - 17 more tools return typed results, so their
  `outputSchema` describes the result (38 tools in all), and every tool parameter carries a
  description in its input schema. Each tool's description also names its result fields,
  because Claude Code shows the model a tool's description and input schema but not its
  `outputSchema`. A test keeps tool docstrings free of `Returns:` sections, which fastmcp drops.
- **Progress notifications** - `build`, the flash tools and the esptool, nrfutil and picotool
  tools report MCP progress while they wait: esptool bytes, UF2 bytes copied, nrfutil packets,
  picotool percent, and the latest build line. A client running them as MCP background tasks
  sees it as the task's status message.
- **Replay favorites and beacon listen** - `NodeRow.is_favorite` reaches `NodeInfo.is_favorite`,
  scripted captures take `is_favorite` on a nodeinfo event, and the sim profile's
  `favorite_fraction` marks favorites. `beacon_listen` (on `replay_start`,
  `screenshot_set_start` and `meshtastic-mcp replay --beacon-listen`) sends
  `ModuleConfig.mesh_beacon` with the listen flag, so the app keeps the passive beacons it
  receives. `beacon_presets` sets the presets a router beacon offers (default `["LONG_FAST"]`)
  and rejects an unknown name.
- **Replay honors client node deletes** - an `AdminMessage.remove_by_nodenum` drops that node
  for the rest of the session: it stays out of the node DB and the LocalStats counts, and its
  streamed and injected packets are dropped.
- **Sim traceroutes follow the map** - relays are picked along the line from source to
  destination, preferring routers and capped by `traceroute_hops`. Per-hop, neighbor and
  node-DB SNR come from a weak-skewed tier model, so links no longer all show one color.
- **`apple` and `sdk` toolsets** - `ios_*` and `screenshot_set_*` are `apple`, `sdk_*` is
  `sdk`, the `rf_*` tools join `rf`, and `serial_*` and `capture_screen` join `hardware`. A test
  pins `core` to an allowlist, so a new tool cannot land there by accident.

### Changed
- **Tool docs reorganized** - descriptions are 28% shorter, with the detail in new docs pages
  (`docs/replay.md`, `docs/firmware-builds.md`, `docs/display-mirror.md`,
  `docs/screenshot-set.md`, `docs/rf-oracle.md`). The new per-parameter descriptions add more
  than that saves, so the tool list is larger than in 0.3.0; `MESHTASTIC_MCP_TOOLSETS` trims it,
  and `core` alone is 32 tools.
- **Tools live in `tools/`** - each toolset's tools are in `src/meshtastic_mcp/tools/<group>.py`
  and `server.py` assembles the app. Every name importable from `meshtastic_mcp.server` still
  is. `tools/list` returns the same tools in a different order.
- **Dependencies** - every locked package with an open security alert is upgraded, Pillow 13
  and filelock 4 among them, and `pydantic` is a declared dependency. Renovate is configured for
  one weekly PR per ecosystem for non-major updates, separate PRs for majors and vulnerability
  fixes at any time. CI's build job builds the web UI and smoke-installs the wheel the way a
  release does.

### Fixed
- **Replay answers want_config as firmware does** - 69420 (`SPECIAL_NONCE_ONLY_CONFIG`) gets
  `my_info`, channels and config; 69421 (`SPECIAL_NONCE_ONLY_NODES`) gets the node's own
  NodeInfo and the node DB; any other nonce gets both. Every nonce but 69420 starts the packet
  stream. Android and Apple send 69420 then 69421. meshtastic-python sends one random nonce,
  which got config with no node DB or packet stream.
- **Replay channels past the eighth** - traffic on a capture channel beyond the eight the config
  advertises is dropped instead of going out on an unconfigured slot.
- **Replay node removal race** - a removal and the final sender check and write share one lock,
  so a removed node's packet cannot slip out.
- **`jobs.poll` after a job ends** - the status is read before the log, so a final status always
  comes with the whole log.
- **`pa_sweep` restore failures** - a sweep that fails and cannot put back `tx_power` or the
  duty-cycle override names the failed restore in its error.
- **`replay_start` profile errors** - a bad `sim_profile` or `beacon_presets` is a `ToolError`.
- **Install-script timeouts** - `erase_and_flash` and `update_flash` raise a `FlashError` when
  the install script times out, instead of a raw `TimeoutExpired`.

## [0.3.0] - 2026-11-17

The first release published since 0.1.0: the 0.2.0 tag never reached PyPI, the GitHub releases or
the MCP Registry, so this release also carries everything listed under 0.2.0.

### Added
- **HTTP serving** - `meshtastic-mcp serve` serves the tools over streamable HTTP from the bench
  host, so every client shares one owner of the serial ports, the recorder and the jobs. Every
  request needs a bearer token of at least 32 characters from `MESHTASTIC_MCP_TOKEN` or a `0600`
  `MESHTASTIC_MCP_TOKEN_FILE`; there is no anonymous mode. See `docs/serve.md`.
- **Toolsets and read-only mode** - `MESHTASTIC_MCP_TOOLSETS` exposes only the named groups
  (`core`, `firmware`, `hardware`, `android`, `discord`, `replay`, `rf`, `local-model`) and
  `MESHTASTIC_MCP_READ_ONLY=1` hides every tool that can change state. A hidden tool cannot be
  called. `hardware` holds the vendor flash tools, uhubctl, `touch_1200bps` and the `nrf52_*`
  tools. See `docs/toolsets.md`.
- **Output schemas** - `list_devices`, `device_info`, `list_nodes`, `logs_window`,
  `packets_window`, `events_window` and `doctor` advertise a typed `outputSchema`.
- **Human confirmation** - a destructive tool called without `confirm=True` asks the user
  through elicitation when the client supports it, and refuses as before when it does not.
  `MESHTASTIC_MCP_CONFIRM=human` asks before an irreversible tool (`factory_reset`,
  `erase_and_flash`, `nrf52_factory_erase`, `vanity_apply`, `atak_fleet_down` with
  `delete_clones`, destructive vendor calls) even when the agent passed `confirm=True`. Every
  confirm-gated tool takes part, and `esptool_raw`, `nrfutil_raw` and `picotool_raw` ask only
  for a destructive subcommand. See `docs/confirmations.md`.
- **Background tasks** - with the new `[tasks]` extra, `build`, `pio_flash`, `erase_and_flash`,
  `update_flash` and `nrf52_flash_uf2` run as MCP background tasks for clients that support
  them and inline for the rest. Canceling a `build` task stops the build; a flash keeps
  writing. See `docs/background-jobs.md`.
- **nRF52 UF2 bootloader tools** - `nrf52_enter_uf2`, `nrf52_bootloader_info`,
  `nrf52_backup_firmware`, `nrf52_flash_uf2` and `nrf52_factory_erase` drive the Meshtastic
  nRF52 bootloader's UF2 drive: enter it over the phone API, read `INFO_UF2.TXT`, back up
  `CURRENT.UF2`, flash a `.uf2` by copy (refusing an image built for the other SoftDevice), and
  wipe saved data where the bootloader takes the factory-erase file. A drive is matched to its
  board by USB serial, so the app's port still finds it in UF2 mode.
- **iOS Simulator app plane** - `ios_list_simulators`, `ios_boot`, `ios_screenshot` (light or
  dark, App Store status bar), `ios_open_url` (deep links and the connect link to a
  `replay_start` node), and with `idb` `ios_ui_dump`, `ios_tap` and `ios_type_text`.
- **Matching Android and iOS screenshots** - `screenshot_set_start` / `screenshot_set_poll`
  serve each app a replay node from one seed, connect both, and capture the same screens by
  deep link in light and dark.
- **Replay `clock_anchor`** - stamps a capture so its last packet lands at a fixed instant, so
  apps connected at different moments show the same last-heard times and node order.

### Changed
- **Failures are tool errors** - a tool that fails raises a tool error, so the result carries
  `isError`, instead of returning `{"error": ...}` as a success. A nonzero exit or a flash to
  the wrong port from `build`, `pio_flash`, `erase_and_flash`, `update_flash` and the esptool,
  nrfutil and picotool tools is a tool error carrying the output tail.
- **Flash tools run as jobs** - `pio_flash`, `erase_and_flash`, `update_flash` and
  `nrf52_flash_uf2` run their write as a `flashes` job and return its `job_id`. Before waiting
  they log the id to the client, so a call that is canceled or times out can follow the write
  with `flash_poll`. `build` runs the job `build_start` would, and its result carries the id.
- **A new replay client preempts the previous one** - as firmware's API server does, so an app
  that opens a second socket for `want_config` gets its node list.

### Fixed
- **Publishing** - the `tak` extra is now the `tak` dependency group. meshtastic-tak is not on
  PyPI, and PyPI rejects a package whose metadata names a git URL, which is what stopped 0.2.0.
  Install the SDK beside a released meshtastic-mcp with the command `doctor` prints, or
  `uv sync --group tak` in a checkout.
- **nRF52 1200 baud touch** - `touch_1200bps` and `nrfutil_dfu` drop DTR while the port is open
  at 1200 baud. meshtastic-python clears `HUPCL` on every open, so after any session an open and
  close alone left the board running its app.
- **`nrfutil_dfu` on nRF52** - it touches the board into serial DFU right before the transfer,
  resolves a `/dev/serial/by-id` link first (the bootloader shows up under a different by-id
  name), passes `--singlebank` as PlatformIO does, and fails unless adafruit-nrfutil printed
  `Device programmed.`, since it exits 0 after "Failed to upgrade target".
- **`erase_and_flash` on a development build tree** - a factory image without its `.mt.json`
  manifest is rebuilt with the manifest target, and the current `mt-<mcu>-ota.bin` is staged
  beside it in place of any stale copy.
- **A wedged board fails the call** - opening a serial interface is bounded by the call's
  timeout plus a grace period. A board whose USB serial has wedged fails with a hint to
  power-cycle it (`uhubctl_cycle`) instead of hanging, and later calls to that port fail fast
  while the stuck open lasts.
- **Gated tools stay off the event loop** - the device, subprocess and sleep work of a
  confirm-gated tool runs on a worker thread, so a DFU or a PA sweep no longer stalls every
  other request.
- **Confirmation answers are bound to their prompt** - an answer counts only for the action it
  was asked about, carried in the sealed `request_state`, and the gate also asks inside a
  background task.
- **Flag values cannot hide a destructive subcommand** - `esptool_raw(args=["--port", "/dev/x",
  "erase_flash"])` is gated like any other destructive call.
- **Failed flash jobs poll as failed** - with their exit code and output, not as `done` with an
  empty log.
- **Builds stop only when asked** - an inline `build` that a client abandons on a timeout keeps
  building, and only an explicit task cancel stops it. A build that runs out of time is killed
  with its whole process group.
- **`android_ui_dump` reads every window** - the `android` CLI now returns one entry per
  window, and the dump flattens them so `android_find_text`, `tap_text` and the ATAK helpers
  find elements again.
- **Replay telemetry comes from the session's node** - a session with a custom node number no
  longer shows a second node.
- **Native meshtasticd build needs jsoncpp** - firmware `develop` (#10152) reads
  `<json/json.h>` from the system jsoncpp, so the CI native build installs `libjsoncpp-dev`
  and `doctor` lists it (`jsoncpp` on Homebrew).

## [0.2.0] - 2026-09-29

### Added
- **MCP Registry listing** - a `v*` tag now also publishes `server.json` to the official MCP
  Registry as `io.github.meshtastic/meshtastic-mcp`, stamped with the tag's version. The
  README carries the `mcp-name` line the registry checks on PyPI to prove ownership.
- **BLE sniffer capture** (`ble_sniffer` capability + one core tool) — watch the
  phone-to-node BLE link from outside both ends with an nRF Sniffer for Bluetooth LE dongle.
  This is the independent oracle for the one failure neither end reports honestly: the app
  says "no devices found" while the node says "advertising". `ble_sniff_start` /
  `ble_sniff_poll` / `ble_sniff_stop` run a capture as a background job (the `jobs.py`
  registry, shared with build/flash/grind), gated on an attached dongle;
  `ble_sniff_status` is **core**, so the tool that reports the hardware missing is not
  hidden by the hardware being missing. Capture shells out to Nordic's
  `nrfutil-ble-sniffer` plugin binary (resolved via `$MESHTASTIC_MCP_BLE_SNIFFER` →
  `$NRFUTIL_HOME/bin` → `~/.nrfutil/bin` → PATH, deliberately *not* through
  `config.nrfutil_bin()`, which also matches `adafruit-nrfutil` and the legacy
  `nordicsemi` pip `nrfutil` — neither has a `ble-sniffer` subcommand); the resulting
  pcap is parsed back with `struct` alone, no scapy/pyshark. Rows merge per advertiser
  address across PDU types because identifying a node is asymmetric — ESP32 nodes
  advertise name and `MESH_SERVICE_UUID` together, nRF52 nodes advertise the UUID but put
  the name in the scan response only — so `likely_meshtastic` keys off the service UUID
  and never off a name. CRC-failing packets are counted but kept out of the rows, a
  captured `SCAN_REQ`/`CONNECT_IND` contributes an address but never its type (scanners
  were measured setting `RxAdd=0` for random-static advertisers), and spans use the pcap
  clock rather than the sniffer's 32-bit microsecond counter, which wraps every ~71 min.
  **Receive only:** the sniffer firmware's UART protocol has no transmit command, so there
  is no BLE counterpart to `inject_frame`; `scan_follow_rsp` (which emits SCAN_REQ to
  solicit names) is the sole transmit path and is off by default so a capture cannot
  perturb what it observes. Output is untrusted — a BLE device name is arbitrary text from
  any radio in range, the widest such source here since it needs no mesh membership at all
  (all four tools `openWorldHint`; see `SECURITY.md`). Docs: `docs/ble-sniffer.md`.
- **Replay client-notification injection** (`replay_inject_client_notification`) — emit the
  device→client `ClientNotification`s real firmware pushes, so an app's notification UI can be
  driven hardware-free: `low_entropy_key` (the pre-2.8 compromised/regenerated-key alert),
  `duplicated_public_key`, the three key-verification steps (`key_verification_number_request`
  /`_number_inform`/`_final`, with `nonce`/`remote_longname`/`security_number`/
  `verification_characters`/`is_sender`), and a plain `text` notification. Marker variants
  default to firmware's wording; `level` maps DEBUG…CRITICAL. Built via
  `build.fromradio_from_kind("client_notification", …)` and delivered through the existing
  `inject_fromradio` path — you can't reproduce these on a healthy device, so this reproduces
  them on demand.
- **Vanity node identities** (`mvgrind` capability + two core tools) — pick a node's id or the
  colour every app paints it, then adopt it. A PKI node's number is
  `crc32(x25519_public_key)` and the clients read the low 24 bits of it straight as RGB
  (Android `NodeColors.kt`, Apple `Color.swift` agree), so a chosen id or colour means grinding
  the keyspace: [mvgrind](https://github.com/miketweaver/mvgrind) does it on the GPU
  (~92 M keys/s on an Apple M4 — a full 8-digit id averages ~48 s, a tolerant colour is
  instant). `vanity_grind_start` / `vanity_grind_poll` / `vanity_grind_stop` drive it as a
  background job (the `jobs.py` registry, now shared with build/flash), gated on the binary
  (`$MESHTASTIC_MCP_MVGRIND` or PATH). `vanity_preview` (key → id + colour + the black/white
  the apps put on it) and `vanity_apply` (write it to a radio) are **core**, so a key ground on
  another machine still applies here. Every hit is re-derived by this repo's own RFC 7748
  X25519 ladder + `zlib.crc32` — no shared code with the grinder's kernels — and comes back
  `verified: false` if the key does not produce the id it claims. `vanity_apply` is
  `confirm`-gated and `destructiveHint` (it *replaces* the identity: the old NodeNum is dropped
  from the node's own DB and peers must re-learn the key), refuses an unclamped key or an UNSET
  `lora.region` (the firmware silently skips keygen there), clears `public_key` on the way out
  so the firmware actually re-derives and moves the NodeNum, and verifies the new number on
  reconnect. Hits are private-key material: `0600` files under the data dir, and returned
  inline — see `SECURITY.md`. `doctor` reports the binary and prints the build command
  (including the one-line macOS `getrandom` patch upstream needs). Full detail:
  `docs/vanity.md`.
- **Discord read-only source** (`discord` capability) — ten `discord_*` tools read the
  Meshtastic community server: server-side `discord_search` (Discord's own index, full
  history; channel / author / mentions / has / date / pinned filters, `offset` paging, `"me"`
  alias), `discord_mentions`, `discord_context` (conversation around a hit or jump link),
  `discord_read` (chronological, `after=<ISO>`), `discord_thread` (bounded, honest
  `truncated`), `discord_forum_posts` (active + archived, tags), `discord_pins`,
  `discord_channels`, `discord_member`, `discord_status`. Every message carries the author's
  top roles and a **`trust` tier** (authoritative / maintainer / contributor / community / bot)
  derived from the guild's role ladder — overridable via `$DISCORD_TRUST_TIERS` — because
  role tracks trust on a community server full of confident misinformation. Operator
  identity (`$DISCORD_USER` / `discord.user`) makes `author="me"` / `mentions="me"` resolve.
  Gated on a bot token (`$DISCORD_BOT_TOKEN` or `<user-config-dir>/meshtastic-mcp/
  discord.token`); stdlib `urllib` against the REST API, no new dependency, no gateway, no
  write endpoint; the bot needs only `View Channels` + `Read Message History`. All tools are
  `readOnlyHint` + `openWorldHint` (public user-authored content — see `SECURITY.md`).
  `doctor` validates the token with one guild lookup. Setup, restricted channels, and the
  shared-org-bot (Developer Team) model: `docs/discord.md`.
- **Richer synthetic node identity** (replay sim) — three improvements to how generated nodes
  look in the app: (1) **better names** via the optional `[sim]` extra (Faker — person names,
  usernames, `<name>'s <device>`, ham callsigns); the sim falls back to its built-in
  adjective+noun pools when Faker is absent, so it only *improves* names and nothing depends on
  it. (2) **Emoji short names** — a large emoji pool with `emoji_short_fraction` (default 0.5)
  of attendee nodes using a single emoji short-name, plus role-**pun** emoji short-names for
  infrastructure (routers get 🗼/📡/🛜/🕸️/…). (3) **2.8 signed node data** — `pki_fraction`
  (default 0.33) of nodes advertise a 32-byte PKC `public_key` in their NodeInfo, and
  `key_verified_fraction` (default 0.05, i.e. verification is rare) of those carry
  `is_key_manually_verified`; and on a **2.8+ edition** (e.g. `firmware_edition="DEFCON"`) the
  **connected/observer node itself is signed** (advertises a stable public key), so the app shows
  the local node signed. All seeded/deterministic and PII-free (every value generated). Node
  identity draws from a dedicated child RNG, so tuning these cosmetic fractions never shifts the
  simulation/traffic stream (the realism bands stay put).
- **Replay local device metrics + local stats** (`replay_start(local_metrics_interval=…)`,
  default 300 s; CLI `--local-metrics-interval`) — the connected/observer node now emits its own
  telemetry every N seconds, like real firmware reporting itself, in **both** variants:
  `DEVICE_METRICS` (battery/voltage/channel-utilization/air-util/uptime → the app's "Device
  Metrics" view) and `LOCAL_STATS` (packets rx/tx, rx-dupe, online/total nodes, noise floor,
  uptime → the app's "Local Stats" / live-activity view). Uptime climbs with the session,
  channel utilization tracks the live replay load (a stress run shows a busy channel), packets-rx
  climbs as the observer receives the streamed feed, online/total nodes = the mesh size, and the
  battery drifts down slowly so the value visibly changes. `0` disables it; `replay_status`
  reports the interval + count.
- **`meshtastic-mcp replay` CLI + `conference-stress` preset** — serve a simulated device
  from the shell with no MCP session: `meshtastic-mcp replay conference-stress --nodes 1600
  --rate 140 --loop` runs the dense-convention stress scenario (gateway-observed density +
  the 17-bot BBS plane, tapback storms, traceroute pairs) in the foreground with periodic
  target-vs-achieved status lines, mDNS-advertised so apps auto-discover it; Ctrl-C stops.
  Accepts any sim preset or a `*.db`/`*.jsonl` capture, `--duration`/`--rate`/`--speed`
  pacing, `--profile` inline-JSON overrides, `--fuzz`, and `--no-mdns`.
- **Replay mDNS/Bonjour advertisement** (`replay_start(mdns=…)`, auto-on for non-loopback
  binds) — the session advertises `_meshtastic._tcp` on `local.` with the `shortname`/`id`
  TXT records real firmware publishes, so apps list it in network discovery (the Apple app
  renders `RPLY_4331`) without manual IP entry. Best-effort backends in preference order:
  the `zeroconf` package if installed, `dns-sd` (macOS), `avahi-publish-service` (Linux);
  none present degrades to an actionable hint under `mdns` in `replay_status`, never a
  session failure. Advertisement is registered after bind (so `port=0` auto-picks are
  advertised correctly) and withdrawn on `replay_stop`.
- **BBS/bot plane for the sim** (`sim_profile={"bots": {...}}`, off by default) — a
  meshing-around-style scene: `count` auto-reply bots (ping→pong pile-ons with bot-to-bot
  chain reactions, cmd/motd/wx/joke/games menus) egged on by attendee nodes, tapback
  (emoji-reaction) storms threading real packet ids via `reply_id` — a power-law long tail
  plus one legendary broadcast collecting `tapback_storm` (default 150) reactions, its body
  carrying the word "tapback" so it's findable in an app search — per-bot advertisement
  beacons, and attendee→bot traceroute request/response pairs. Emitted after the RF-observer
  stage (bots sit at the venue core; the scene arrives intact) on an isolated child RNG.
  `build.from_kind("text", ...)` accepts `reply_id` + `emoji` so `replay_inject` can fire a
  tapback on demand.
- **Replay `duration` pacing** (`replay_start(duration=…)`) — compress the whole (windowed)
  capture into a fixed wall-clock span by deriving a steady rate of `packets / duration`
  (e.g. `duration=150` replays an entire DEF CON capture in 2.5 minutes regardless of packet
  count). Takes precedence over `rate`. `replay_status` now reports `target_rate` and the live
  `achieved_rate`, so a stress run is self-verifying.

### Fixed
- **Replay sends every channel slot** - want-config sent only the capture's channels, so the
  Apple app refused the incomplete set and showed nodes but no messages. Unused slots now
  arrive `DISABLED`, as they do from a radio.
- **`discord_channels` listed channels the bot cannot read** — `GET /guilds/{id}/channels`
  returns the whole guild regardless of the bot's permissions, so the tool advertised every
  channel while claiming to list "readable" ones. On the Meshtastic server that meant all 235,
  including the private management channels; an agent read the listing, concluded it could see
  `#leads`, and only found out when `discord_read` came back `403 Missing Access` — or, worse,
  concluded from an empty `discord_search` that nobody had discussed something. The listing is
  now filtered against per-channel permissions computed from the overwrites already in that
  payload (@everyone, then the bot's role overwrites, then its member overwrite, with
  `ADMINISTRATOR` short-circuiting), threads inherit their parent's readability, and the
  withheld count comes back as `unreadable`. `include_unreadable=True` lists them anyway with
  `readable`, which is how you see what to ask an admin to grant. Name resolution deliberately
  still searches the unfiltered set, so `discord_read("leads")` keeps failing with Discord's
  own 403 rather than a misleading "no channel named".
- **Flash tools can no longer flash a different device than the requested port**
  (`flash`/`pio_flash`, `flash_start`, `erase_and_flash`, `update_flash`) — pioarduino's
  Hybrid-Compile pass (custom sdkconfig → `*** Compile Arduino IDF libs ***`) re-invokes a
  child `pio run -e <env> -t upload` that forwards targets but no CLI options, so the child
  dropped our `--upload-port`, auto-detected a port, and could flash whichever device it
  found first (real incident 2026-08-25: a `meshnology_w10` 16 MB image landed on the 8 MB
  Heltec Wireless Tracker V2 one hub over, boot-looping it while the job reported success).
  Uploads now also pin the port via `PLATFORMIO_UPLOAD_PORT` in the subprocess environment —
  the project-option override the child run inherits — and every flash result is post-checked
  against the ports the output claims were used (`Auto-detected:` / `Using manually
  specified:` / esptool's `Serial port` lines): a mismatch forces a non-zero exit with an
  `upload_port_mismatch` error naming both ports. Empty or glob `port` arguments (which
  PlatformIO silently turns into auto-detection) are rejected up front, and `flash_start`
  jobs now also apply the silent-DFU-failure detection `flash` already had, surfacing
  `error` through `flash_poll`. Uploads also take the same per-port lock the
  connect/serial paths use, so they serialize against each other and against a live
  `connect()` — held across `flash`'s pre-flight too, since `ensure_port_free` reads an
  in-process holder as a wedged device and would power-cycle the hub slot mid-flash.
  A busy port fails fast (`flash_start` included, on the calling thread).
- **Traceroutes now surface in app traceroute logs** — the sim emitted only in-flight
  traceroute *requests* (`request_id` 0), which apps ignore: their traceroute logs persist
  only *responses* (nonzero `decoded.request_id`), so a whole capture streamed with an empty
  traceroute log (found live against the Apple app). The sim now emits request → response
  pairs, and `RouteDiscovery` payloads follow firmware semantics everywhere (sim + the live
  traceroute responder): `route`/`route_back` carry intermediate relays only — endpoints are
  implied by the packet from/to, so listing them drew duplicated hop lists — SNR lists carry
  one entry per receiving hop (`len(route) + 1`, SNR×4), and responses set `hop_start > 0`
  (apps gate route-back rendering on it). `build.from_kind("traceroute", ...)` accepts
  `request_id` to craft responses; the traceroute block runs on an isolated child RNG so
  future changes there can't reshuffle other sections' seeded draws.
- **Replay survives mid-stream client disconnects** — the stream thread treated any send
  failure as session-fatal (`stop.set()`), so an app closing or resetting the connection
  mid-pass tore down the whole listener even with `loop=true` (found live: a real app
  disconnect ended a stress session with `Connection reset by peer`). The stream now severs
  only its own connection (a `shutdown()` that also unblocks the reader for the
  stalled-but-connected send-timeout case); the accept loop keeps listening and a reconnect
  handshakes and streams again. `achieved_rate` re-anchors per connection so reconnect gaps
  don't dilute it.
- **Replay pacing drift** — the stream loop slept a fixed `1/rate` per packet, so per-packet
  protobuf work and OS wait-overshoot accumulated on top of every interval and the achieved
  rate sagged ~15–25% below target (requesting 120 pkt/s delivered ~99 pkt/s; the gap widened
  with rate). Pacing (both `rate` and `speed` modes) is now anchored to absolute deadlines, so
  the requested rate is delivered exactly up to the raw send ceiling (thousands/sec).

## [0.1.0] — 2026-07-02 (first public release)

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
