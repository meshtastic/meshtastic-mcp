# FleetSuite Nightly Bake

The nightly bake is FleetSuite's unattended overnight pipeline. Every night it:

1. **Self-updates meshtastic-mcp** — `git pull --ff-only origin master` on this checkout,
   reinstalls, rebuilds the SPA when `web-ui/` changed (into a temp dir, atomically swapped),
   then restarts itself; launchd respawns it and the scheduler resumes the night where it
   left off. A failed reinstall rolls back to the previous sha.
2. **Updates the nightly firmware checkout** — a dedicated clone of `meshtastic/firmware`
   that the nightly owns and hard-resets to `origin/develop` (`fetch` → `checkout -f` →
   `reset --hard` → `submodule update`). Your own working trees are never touched.
3. **Pre-builds** every board env the online fleet needs (sha-keyed artifact cache), so a
   broken build surfaces as a report line before any hardware is flashed.
4. **Bench-checks** — offline expected devices get the safe recovery ladder
   (reboot → power-cycle) before the suite starts.
5. **Runs the full suite** via the bench test runner: the bake tier flashes every board with
   the private test profile (channel `McpTest`, slot 88, seeded PSK, region US — never
   LongFast defaults), then all hardware tiers run.
6. **Soaks the mesh for 2 h** (configurable): serial logs from every board tee into
   per-night JSONL files, a sequenced test message is injected on an interval (so mesh
   delivery is measurable), and assigned cameras grab periodic screen snapshots. A soak
   preflight reads each board's live config and raises a loud `channel.default_profile`
   error if any board sits on the default channel.
7. **Recovers the bench** — anything offline or wedged at the end of the night gets the full
   ladder including a DFU reflash (config-gated), so the bench is healthy by morning.
8. **Analyzes + reports** — deterministic heuristics (panics, error bursts, reboot churn,
   battery/heap slope, lost soak traffic, missing/unbaked devices, firmware version diff)
   plus, when a local model is reachable, a behavioral pass: chunked map-reduce summaries of
   the night's logs and a vision/OCR check of each snapshot. The report posts as a GitHub
   issue and is always stored locally (Nightly tab → history → expand).

Every step failure becomes an *observation*, never a silent abort — the report is written
even when the pipeline itself breaks (`PIPELINE FAILED at <step>` issues).

## Deployment (Mac Mini)

```bash
# one-time
gh auth login                       # the report posts via your gh keyring auth
./scripts/install-launchd.sh        # LaunchAgent: KeepAlive + RunAtLoad + supervisor
./scripts/install-menubar.sh        # optional: 🟢/🟡/🔴 menu-bar start/stop/status ([menubar] extra)
open http://127.0.0.1:8765          # Nightly tab → enable, set time, set report repo
```

The menu-bar controller (`meshtastic-mcp-menubar`, macOS-only) is a convenience,
not part of the service — it just drives the same `com.meshtastic.fleetsuite`
agent. "Stop" *unloads* the agent (a plain kill would be respawned by `KeepAlive`).

The LaunchAgent runs `scripts/fleetsuite-supervisor.sh`, which wraps
`fleetsuite.sh --browser` with **crash-loop rollback**: three consecutive starts dying
within 60 s roll the meshtastic-mcp checkout back to the last sha that ran ≥ 120 s and
reinstall it. `--browser` mode is mandatory under launchd — the pywebview desktop window
cannot be SIGTERM-restarted.

Environment (set in the plist):

| var | value | why |
|---|---|---|
| `MESHTASTIC_MCP_NIGHTLY_FW_DIR` | `~/.meshtastic_mcp/nightly-firmware` | the checkout the nightly may hard-reset |
| `MESHTASTIC_FIRMWARE_ROOT` | same path | builds/bakes compile exactly what the nightly pulled |
| `FLEETSUITE_EXTRAS` | `web,ui` | a clean redeploy installs the camera + OCR deps (soak snapshots), not just `web` |
| `FLEETSUITE_HOST` | `127.0.0.1` | web-server bind address. `0.0.0.0` exposes the UI on the LAN — **no auth, trusted networks only** (`FLEETSUITE_HOST=0.0.0.0 ./scripts/install-launchd.sh`) |
| `PATH` | homebrew + platformio | launchd jobs get a bare PATH |

Optional: `MESHTASTIC_MCP_SOURCE_ROOT` (mcp checkout override, default: this repo),
`MESHTASTIC_MCP_ARTIFACT_DIR` (build cache), `MESHTASTIC_MCP_LOCAL_*` (local-model backend).

## Prerequisites

- **gh** installed and logged in (`meshtastic-mcp doctor` → `gh-auth` check). The default
  report repo is `thebentern/fleet-nightly`; create it private, or enable *auto-create*.
  **GitHub posting is off by default** — reports are always rendered and stored locally; flip
  "post issues" on in the Nightly tab once the repo is set.
- **Ollama** (or a llama-server) for the behavioral analysis — optional; without it the
  deterministic report still posts and carries an `llm_unavailable` observation. The
  *auto-start local LLM* toggle tries `llama-server` bootstrap once per night.
- **Cameras** (optional): add + assign them in the Fleet tab for soak snapshots; enable
  screen keep-alive so OLEDs stay lit. Camera capture needs the `[ui]` extra (opencv) — the
  deployment plist's `FLEETSUITE_EXTRAS=web,ui` installs it on a clean deploy; macOS also
  gates camera capture behind a Camera privacy grant for the launchd process.

## Data & retention

Per-night data lives in `~/.meshtastic_mcp/nightly/<id>/` (`soak-logs.jsonl`,
`soak-telemetry.jsonl`, `soak-sends.jsonl`, `snap-*.jpg`). History beyond `keep_nights`
(default 30) is pruned after each report, along with firmware-artifact trees only old
nights reference. Reports (title + full untruncated body + delivery status) are in the
`nightly_reports` table; failed deliveries can be re-sent from the UI (*repost*).

## First night expectations

The first run clones `meshtastic/firmware` (~10 min) and cold-builds every fleet env —
budget an extra half hour. Default schedule is 01:30. A clean night (suite + 2 h soak)
typically finishes by ~07:30; prebuild, recovery, analysis, and reporting add to that, and
the whole run is bounded only by `pipeline_timeout_h` (default 10 h). Set the schedule so
this window doesn't collide with daytime bench use.

## Security note

Device log excerpts embedded in reports are authored by remote mesh nodes — **untrusted
content**. Reports are rendered for human reading (evidence fenced, GPS scrubbed, plain-text
preview in the UI); never feed a report body to an agent as instructions. Keep the report
repo private.
