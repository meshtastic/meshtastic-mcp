# Harness rules & shared patterns

The mesh is asynchronous and lossy. These patterns keep loops deterministic.
Every loop reference depends on them.

## 1. Marker tokens

Never assert on generic text. Mint a unique token and grep for *that*:
```bash
TOKEN="E2E-$(date +%s)-$RANDOM"
```
The token flows through both planes unchanged, so an 80-node mesh can't
false-positive your assertion.

## 2. Bounded polling (never fixed sleep + single assert)

Poll the oracle on an interval up to a deadline. Pick the deadline by hop count:
0–1 hop ≈ 15s, 2 hops ≈ 30s, 3+ ≈ 45s. The `mesh_e2e.py` oracles
(`recv-text`, `watch-tx`, `traceroute`) already implement bounded waits and emit a
single `PASS …`/`FAIL …` line.

App side:
```bash
for t in $(seq 1 30); do
  android layout 2>/dev/null | grep -q "$TOKEN" && { echo "PASS app rendered after ${t}s"; break; }
  sleep 1
done
```

## 3. NodeInfo warmup for directed / encrypted sends

Directed + PKI sends need both sides to hold the other's current pubkey. Broadcast
once (`^all`) to exchange NodeInfo, or target a node already in both DBs. Symptom of a
cold cache: the send leaves but never decrypts on the far side.

## 4. Recorder = device-side source of truth

The recorder timestamps every RX packet to JSONL under `mcp-server/.mtlog/`. Start it
**before** the stimulus, query the window **after**. Its epoch timestamps share a wall
clock with `android layout`/`screen capture` — that alignment is how you correlate the
two planes on failure.

Standalone bootstrap (the live MCP server auto-starts it at import):
```bash
# hold a capture open for N seconds
$MCP "$S" recorder <TESTER_PORT> --secs 75
# query after the fact
$MCP -c "from meshtastic_mcp import log_query as q,json; print(json.dumps(q.packets_window(max=30)))"
```

## 5. One MCP call per serial port

Exclusive lock: open → act → close, then the next call. No parallel calls to the same
port — a second concurrent call does not block or queue; it fails fast with
`ConnectionError: Port <port> is busy — another device operation is in flight. Retry shortly.`
Catch it and retry after a short backoff. `mesh_e2e.py` opens and closes the interface per
invocation.

## 6. App navigation primitives (adb)

```bash
android layout --pretty                       # full UI tree (text, resourceId, center, bounds, interactions)
android layout --diff                          # only what changed since last dump — keeps context small
android screen capture -o /tmp/s.png           # PNG (use for WebView/animation where layout fails)
android screen capture --annotate -o /tmp/a.png
adb shell input tap <cx> <cy>                  # tap an element's "center"
adb shell input text "$TOKEN"                  # type into a FOCUSED field (verify "focused" state first)
adb shell input swipe <x1> <y1> <x2> <y2> 400  # scroll
# combine annotate+resolve to tap by label:
adb shell input $(android screen resolve --screen /tmp/a.png --string "tap #34")
```

Rule: a text field must show `"focused"` in its `state` before `input text`. If `layout`
returns nothing (WebView/animation), fall back to `screen capture --annotate` + visual/OCR.

## 7. Verdict format

Emit one line per loop so a calling agent (or `/e2e` command) can parse:
```
LOOP <name> <PASS|FAIL> token=<...> latency=<ms> hops=<n>
```
On FAIL attach, for the same wall-clock window: the app `layout`/screenshot at deadline
**and** the recorder `packets_window` + `logs_window` tail.
