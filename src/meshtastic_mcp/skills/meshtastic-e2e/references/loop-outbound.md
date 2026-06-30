# Loop: outbound message (app → device)

Type+send a text in the app; assert the wire truth on the tester radio.

**Stimulus:** `adb shell input` types the token in the app and taps send  
**Oracle:** recorder `packets_window` shows a `TEXT_MESSAGE_APP` from the DUT carrying `token`

## Steps

```bash
export MESHTASTIC_FIRMWARE_ROOT="$HOME/meshtastic/firmware"
MCP="$MESHTASTIC_FIRMWARE_ROOT/mcp-server/.venv/bin/python"
S="$HOME/.agents/skills/meshtastic-e2e/references/mesh_e2e.py"
TESTER=/dev/cu.usbmodem101
TOKEN="E2E-$(date +%s)"

# 1. Start the device oracle in the background: hold the tester open + recorder capturing,
#    while concurrently watching the recorder for the token.
$MCP "$S" recorder "$TESTER" --secs 60 >/tmp/rec.json 2>/dev/null &
REC=$!
sleep 3   # let the interface come up and the recorder subscribe

# 2. App stimulus: focus compose field, type token, tap send.
android layout --pretty >/tmp/ui.json
#   find the focusable compose field + the send button centers from /tmp/ui.json, e.g.:
#   jq -r '.[] | select(.interactions|index("focusable")) | .center' /tmp/ui.json
adb shell input tap <compose_cx> <compose_cy>     # field must report "focused"
adb shell input text "$TOKEN"
adb shell input tap <send_cx> <send_cy>

# 3. Device oracle: poll the recorder for the token (bounded).
$MCP "$S" watch-tx "$TOKEN" --secs 30              # prints PASS/FAIL line
wait $REC 2>/dev/null
```

## Notes

- `watch-tx` greps `packets_window` for a `TEXT_MESSAGE_APP` packet whose JSON contains the
  token. The recorder summarizes payloads as hex + matched text; a plaintext channel shows the
  token directly. For an **encrypted DM to the tester**, the tester must be the destination and
  hold the DUT's pubkey — otherwise the recorder logs the packet but can't surface the text.
- Two processes touch the tester port here: the `recorder` invocation owns the SerialInterface;
  `watch-tx` only reads the recorder JSONL (no port access), so there's no lock conflict.
- Best practice: send to the **tester's node id** (directed) so delivery is unambiguous, after a
  NodeInfo warmup broadcast.

## Failure triage

- Token never appears in `watch-tx` → did the app actually transmit? Check `adb` taps hit the
  right centers (`android layout --diff` right after send should show the sent bubble in the app).
- App shows the sent bubble but tester never logs it → **RF/channel** (region/channel mismatch,
  out of range, hop limit) — confirm with a `traceroute` between the two radios.
