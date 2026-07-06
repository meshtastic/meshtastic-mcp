# Plan: calibrating `replay/sim.py` against real event captures

Goal: make the synthetic mesh generator produce traffic that is statistically
indistinguishable from what a real gateway heard at Burning Man 2025 and
DEF CON 33, while keeping every parameter tunable and every output seeded,
synthetic, and PII-free.

## Ground truth

Two private datasets, cloned to `~/meshtastic/datasets/` (do **not** vendor any
raw records into this repo — aggregates only):

- **Burning Man 2025** — `garthvh/burningmesh-replay`: SQLite
  (`packet` / `packet_seen` / `node`), 156k decoded MeshPackets, 1,606 nodes,
  10 days. Our `capture.from_sqlite()` already reads this schema directly.
- **DEF CON 33** — `darknet-ng/DEFCON33-Meshtastic-Traffic`: gateway
  `str(dict)` text logs from Pi Zero + SX1262 gateways (LongFast +
  ShortTurbo planes), 181k records, 1,831 senders, ~186 h.

Measured comparison (sim = `generate(nodes=1600, days=3)`), analysis script
currently at `/tmp/mesh_stats.py`:

| Metric | Burning Man | DEF CON 33 | Sim today |
|---|---|---|---|
| Portnum mix | NODEINFO 38%, POS 34%, ROUTING 12%, TEL 11%, TEXT 2.9% | NODEINFO 34%, POS 31%, TEL 24%, TEXT 9% | TEL 48%, POS 28%, NODEINFO 1.3%, ROUTING 0.08% |
| Pkts/hour (gateway view) | 643 | 977 | 4,919 |
| Position interval p50 | 583 s | — | 164 s |
| Inter-arrival p99 / max | 33 s / 2,308 s | 43 s / 2,213 s | 5 s / 18 s |
| Duplicate packet IDs | — | 42% seen ≥2× | 0% |
| Encrypted fraction | 0 (pre-decoded) | 46% (40.7% via_mqtt) | 16.6% |
| hop_start | 4 (62%), 3 (28%), 7 subpop | 3 (88%), 7 subpop | 3/4/5 only |
| Text len p50/p90/max | 25 / 100 / 227 | 17 / 78 / 246 | 10 / 57 / 66 |
| Text DM fraction | 3.4% | ~0 (PKI-invisible) | 11.9% |
| rx_snr / rx_rssi p50 | 3.0 / −100 | 10 / −84 | not modeled |
| Routers per ~1600 nodes | 6–13 | ~6 | 66 |
| Talker skew (top 10% share) | 47% | 67% | 78% |
| Battery | per-node discharge, 0% + 101 modes | — | i.i.d. per packet |
| chutil | p50 7.4, max 39 | — | p50 15.2, max 75; i.i.d. |

Two structural gaps explain most rows:

1. **Observer gap** — the sim emits "all traffic that exists"; the datasets are
   *what one gateway heard*: RF-lossy, duplicated by rebroadcast (copies with
   decremented `hop_limit`, distinct SNR/RSSI, `relay_node`), and stamped with
   `rx_snr`/`rx_rssi`/`priority`/`via_mqtt`.
2. **Social gap** — no NodeInfo want_response exchanges (arrival storms), no
   ACK economy, no conversation bursts, no per-node temporal state (battery
   curves, chutil coupled to airtime).

## Workstreams

### WS0 — Metrics harness + golden stat fixtures ✅ *(done)*

Shipped:

- `src/meshtastic_mcp/replay/metrics.py` — `capture_stats(cap)` (single-pass
  stat schema incl. telemetry variant mix, env-field presence, TAK counter)
  and `sqlite_extra_stats(db)` (observation-level dup multiplicity + RX
  SNR/RSSI populations that `from_sqlite`'s per-packet dedupe hides).
- `tools/import_defcon_logs.py` — streaming `str(dict)`-log → shared SQLite
  schema importer (181,279/181,281 DC records parsed; duplicates preserved as
  `packet_seen` rows: 117,676 packets / 181,278 observations).
- `src/meshtastic_mcp/replay/profiles/{burningman2025,defcon33}.json` —
  aggregate stat profiles (8 KB each; leak-checked: no payloads, names,
  channel names, keys, or coordinates). **Generated locally, gitignored —
  dataset-derived files are not committed.** Regression baselines + WS4
  calibration inputs; profile-dependent tests skip when absent. WS4 presets
  therefore encode their tunables as reviewed constants in code (as
  `sim.PROFILE` already does), not by shipping these files.
- `tests/unit/test_replay_metrics.py` — schema/determinism, importer
  round-trip on a synthetic log in the DC format, profile integrity, and
  calibration-target assertions (DC dup rate > 20%, BM NODEINFO > TELEMETRY,
  ROUTING > 5%, text max > 200).
- Deferred: a `capture-stats` CLI subcommand (trivial once needed).

Local (not committed): real datasets in `~/meshtastic/datasets/`, imported
DBs at `/tmp/{burningmesh,defcon33}.db`, first-pass analysis in
`~/meshtastic/datasets/analysis/`.

### WS1 — Traffic economy fixes in `sim.py` *(fix the portnum mix + volume)*

- **NodeInfo exchange storms:** on node join, emit broadcast NodeInfo **plus**
  k ∈ [2, 8] want_response request/reply pairs with nearby nodes (drawn by
  cluster); plus a steady background of exchange pairs proportional to churn.
  Target NODEINFO ≈ 30–40% of decoded traffic.
- **ACK economy:** ROUTING packets generated as a function of DM/want_ack
  traffic and traceroutes, not a fixed trickle. Target ≈ 5–12%
  (`PROFILE["ack_ratio"]`).
- **Decouple beacons from `talk`:** the lognormal talk multiplier applies to
  social traffic (text, exchanges) at full strength but position/telemetry
  cadence at dampened strength (e.g. `talk**0.3`). Target position interval
  p50 ≈ 10 min; telemetry share ≤ 25%.
- **Role mix:** routers ≈ 0.4–0.8% of nodes (6–13 per 1600, keep the named-8
  cap); add `ROUTER_CLIENT` (legacy) and `CLIENT_HIDDEN` to `role_weights`.
- **Text realism:** three-component length model (short one-liners ~60%,
  themed lines, long-tail 120–240-char "wall of text" bursts); DM fraction
  ↓ to ~3%; hop_start subpopulation at 7 (`PROFILE["hop_start_weights"]`).
- **Small fidelity:** encrypted packets get payload lengths sampled from the
  portnum mix and a hash-like channel byte (not 0); decoded packets get
  `priority` (BACKGROUND for beacons/telemetry, DEFAULT/RELIABLE for text)
  and `bitfield`; precision_bits gains 0 and 15 buckets.

**Acceptance:** portnum mix within ±8 pp of the blended real mix per port;
text p50/p90/max within tolerance; all existing tests still pass
(`test_all_sim_data_is_synthetic` unchanged — no real strings introduced).

### WS2 — Observer / RF model *(new module, biggest realism win)*

New `src/meshtastic_mcp/replay/observer.py`, applied as a final pipeline stage
inside `generate()` (pre-serialization, so it's cheap) and exposed as a pure
function for tests:

- Observer placed at a venue coordinate (`PROFILE["observer"]`), default =
  venue center.
- Per emitted packet, from sender position: log-distance path loss
  `RSSI = tx_power − PL(d, n) + N(0, σ)`; SNR derived from RSSI vs noise
  floor; reception probability = sigmoid around sensitivity. Lost packets are
  dropped from the capture (this **is** the volume calibration).
- Rebroadcast duplicates: k extra copies (weights fit from DC: 1×58%, 2×?,
  3×~24%, tail to 8) with decremented `hop_limit`, per-copy RSSI/SNR redrawn
  through a random relay position, `relay_node` = low byte of a plausible
  relay, small time offsets (0.2–5 s).
- `via_mqtt` subpopulation (`mqtt_fraction`, DC ≈ 0.4): MQTT-heard copies get
  no SNR/RSSI, full hop fields — models the bridged plane.
- Serialized packets now carry `rx_snr`/`rx_rssi`/`rx_time`/`relay_node`;
  engine change: `_stream()` keeps restamping `rx_time` but must **not**
  clobber the rest (it already doesn't — verify with a test).
- Tunables: `enabled`, `position`, `tx_power`, `path_loss_exp`, `sigma_db`,
  `noise_floor`, `dup_weights`, `mqtt_fraction`, `loss_floor`. `enabled=False`
  returns today's omniscient view (back-compat; default **on** for presets,
  off for bare `generate()` to keep existing tests deterministic-stable).

**Acceptance:** observed pkts/hr within 2× of real for 1600 nodes; dup-ID
multiplicity and SNR/RSSI percentiles within tolerance of the DC profile;
hop_limit spread matches (0..hop_start all populated).

### WS3 — Temporal coherence *(per-node state machines)*

- **Battery:** per-node mode (plugged→101, discharging with a rate, solar
  recharge for infra) evolved across the event; a fraction hit 0 and go
  silent (ties into presence/churn). Kills the i.i.d. draws.
- **chutil from actual airtime:** compute per-hour generated airtime
  (payload size → airtime at the active preset) and derive
  channel_utilization telemetry from it (+noise), so chutil tracks the
  diurnal envelope like reality.
- **Bursty text:** self-exciting process — each text raises reply probability
  on its channel for ~5 min (target inter-arrival p99 ≈ 30–45 s with a
  long-silence tail, vs today's uniform smear); occasional scripted spikes
  hookable by presets (keynote, emergency).

**Acceptance:** inter-arrival p99/max within tolerance; battery histogram
bimodal (101 + curve + 0-spike); chutil correlates with hour-of-day traffic
(ρ > 0.6).

### WS4 — `fit_profile()` v2 + scenario presets

- Extend `fit_profile()` to also fit: 24-bin diurnal envelope, hop_limit/
  hop_start hists, talker-skew lognormal params, text-length histogram,
  encrypted fraction, per-role portnum rates, dup multiplicity, SNR/RSSI
  params, ack ratio — i.e. everything `generate()` now consumes. Output is
  exactly the WS0 profile JSON schema.
- `generate(profile=...)` accepts the JSON (file path or dict).
- Presets: `replay_start(source="burningman")` / `source="defcon"` resolve to
  `generate(profile=<preset dict>)` in `_load_replay_capture()` (server.py
  ~line 1916) — preset tunables are reviewed constants in code (informed by
  the local profiles, not shipping them), with venue geo + channel lineups +
  scripted spikes
  (BM flood-emergency arc; DC con-hours envelope, ShortTurbo second plane,
  46% encrypted, mqtt_fraction 0.4). `meshcon` stays the default.
- Fuzz tie-in: a `ninja` preset in `fuzz.py` — NodeInfo spoof floods
  (rewrites long_name/emoji of recently-seen nodes, replays with correct
  node num), modeled on the DC 33 attack. Composes with presets:
  `replay_start(source="defcon", fuzz="ninja")`.

**Acceptance:** `generate(profile=fit_profile(from_sqlite(real_db)))` scores
within tolerance of that capture on the WS5 harness; `replay_start`
docstring/tests updated; tool schema unchanged otherwise.

### WS-T — Realistic sensor telemetry *(with WS3)*

Measured from the BM capture (now in the golden profile):

- Variant mix: device_metrics 85%, power_metrics 8.6%, environment_metrics
  6.5%; **57 env senders** (~3.5% of nodes), each emitting a *consistent
  field subset* — temperature always, **lux 63%**, humidity 36%, pressure
  17%, gas_resistance + IAQ 3.4%.
- Temperature 8.2–54.8 °C, p50 20.2 — a desert diurnal swing, not a uniform
  draw. Humidity contains **NaN** values (real sensors emit NaN; clients must
  cope). Pressure is bimodal ~782 vs ~1012 hPa (station-altitude vs
  sea-level-corrected devices).

Sim today: env telemetry only from SENSOR-role + 3 router nodes at a flat
30-min cadence, uniform 12–33 °C / 8–40 %RH / 770–790 hPa, no lux/IAQ, no
diurnal coupling, no NaN. Plan:

- **Sensor personas**: each env node draws a device class once (temp-only /
  +lux / BME280 → +humidity+pressure / BME680 → +gas+IAQ / INA → power) and
  emits that field subset consistently.
- **Physical venue model** (shared with WS3's clock): temperature = diurnal
  sinusoid (mean/amplitude in `PROFILE["climate"]`; playa preset ≈ 10–50 °C),
  lux from solar elevation (0 at night), humidity anti-correlated with
  temperature, pressure = slow random walk around the venue-altitude mode.
- **NaN injection knob** (small fraction of humidity/pressure readings).
- **Power metrics** as a first-class stream (~9% of telemetry, ch1–ch3).
- `fit_profile` v2 fits variant mix / env-field presence / temp range —
  already captured by `capture_stats`, so the fit is a pass-through.

**Acceptance:** variant mix within ±3 pp of profile; env-field presence
ratios match; temperature tracks the diurnal envelope (ρ > 0.7 with the
climate model); a NaN fraction survives serialization.

### WS-A — ATAK / TAKPacket traffic *(opt-in scenario layer)*

Fact from the data: **zero** ATAK packets (portnum 72/257) in either real
capture — so TAK traffic is an opt-in scenario knob (default off), not part
of the fitted event profiles. Purpose: exercise app/plugin TAK paths
(ATAK plugin, forwarder, map PLI rendering) against the simulated mesh.

- `PROFILE["tak"] = {"team_nodes": 0, "pli_interval": 45, "chat_per_hour": 2,
  "team": "Cyan", ...}` — when `team_nodes > 0`, a squad of nodes with
  callsigns emits `TAKPacket` (portnum 72, `atak_pb2`): PLI
  (lat/lon/alt/speed/course following the node's mobility track), GeoChat
  (to team / broadcast), and status (battery follows the WS3 battery state).
  Core stays dependency-light — legacy TAKPacket protobuf needs nothing new.
- **V2 wire format**: `meshtastic/TAKPacket-SDK` (Python impl; CoT XML →
  TAKPacketV2 + zstd dictionary compression, median 87 B payloads) behind an
  optional `[tak]` extra. When installed, the sim can emit wire-compressed V2
  payloads and we validate byte-compat against the SDK's 47 shared fixtures;
  without it, legacy protobuf only.
- Fuzz composition: malformed/oversized CoT and truncated compressed payloads
  as a `fuzz` preset for parser robustness.
- `capture_stats` already counts `tak_packets`, so regression coverage is
  free once generation lands.

**Acceptance:** with `team_nodes=4`, the app-visible mesh shows a moving TAK
squad (PLI cadence ±20%, chat on the right channel); default profiles remain
TAK-free; `[tak]` extra round-trips an SDK fixture.

### WS5 — Regression validation

- `tests/unit/test_sim_realism.py`: generate (seeded, ~400 nodes, 2 days,
  observer on) → `capture_stats` → assert each metric inside tolerance bands
  derived from the two golden profiles (bands stored alongside the JSONs).
  Runs in the portable tier (no hardware/firmware).
- Keep `test_sim_is_seeded_...` byte-for-byte determinism: all new randomness
  must draw from the same seeded `rng`.

## Sequencing & effort

| Phase | Depends on | Size |
|---|---|---|
| WS0 metrics + fixtures | — | ✅ done |
| WS1 traffic economy | WS0 | M |
| WS2 observer model | WS0 | M |
| WS3 temporal coherence | WS1 | M |
| WS-T sensor telemetry | WS3 (climate/clock model) | S–M |
| WS4 fit v2 + presets | WS1+WS2 | M |
| WS-A ATAK layer | WS3 (mobility/battery), opt-in | S–M |
| WS5 regression tests | all | S |

WS1 and WS2 are independent after WS0 and can proceed in parallel.

## Invariants (do not break)

- Seeded determinism: `(seed, nodes, days, start)` → byte-identical capture.
- 100% synthetic output: fixtures are aggregates only; no real node names,
  texts, keys, or coordinates enter the repo (`test_all_sim_data_is_synthetic`).
- Core stays dependency-light; metrics/observer use stdlib + existing protobufs.
- `generate()` default signature behavior unchanged for existing callers.
