# Loop: inbound message (device → app)

A mesh peer sends a text; assert the app renders the bubble.

**Stimulus:** TESTER radio `sendText(token)`  
**Oracle:** `android layout --diff` over the app's messages screen shows `token`

## Steps

```bash
export MESHTASTIC_FIRMWARE_ROOT="$HOME/meshtastic/firmware"
MCP="$MESHTASTIC_FIRMWARE_ROOT/mcp-server/.venv/bin/python"
S="$HOME/.agents/skills/meshtastic-e2e/references/mesh_e2e.py"
TESTER=/dev/cu.usbmodem101
TOKEN="E2E-$(date +%s)"

# 0. App: open the channel/DM the tester will post to (so the bubble is on-screen).
#    Navigate via adb taps; confirm with `android layout` you're on the messages list.

# 1. Establish a baseline UI snapshot (so --diff isolates the new bubble).
android layout --diff >/dev/null

# 2. Device stimulus: broadcast the token from the tester radio.
$MCP "$S" send "$TESTER" "$TOKEN"          # add --dest '<DUT node id>' for a directed DM

# 3. App oracle: bounded poll for the token.
PASS=0
for t in $(seq 1 30); do
  if android layout --diff 2>/dev/null | grep -q "$TOKEN"; then
    echo "LOOP inbound PASS token=$TOKEN latency~${t}s"; PASS=1; break
  fi
  sleep 1
done
[ "$PASS" = 1 ] || echo "LOOP inbound FAIL token=$TOKEN (not rendered in 30s)"
```

## Notes

- **Directed DM:** add `--dest '<DUT !nodeid>'` to `send`. Warm up NodeInfo first
  (`harness.md` §3) or the encrypted DM won't decrypt on the phone.
- **Broadcast:** lands in the primary channel; make sure the app is viewing that channel.
- If `layout` is empty (compose animation/WebView), fall back to
  `android screen capture --annotate -o /tmp/a.png` and inspect/OCR for the token.

## Failure triage

On FAIL, capture both planes for the same window:
```bash
android screen capture -o /tmp/inbound_fail.png
$MCP -c "from meshtastic_mcp import log_query as q,json; print(json.dumps(q.packets_window(max=20)))"
```
- Token present in `packets_window` but not in the app → **app-side** bug (parse/render/wrong channel).
- Token absent from `packets_window` too → **device/RF** problem (the send never reached the DUT;
  check region/channel match, hop count, NodeInfo warmup).
