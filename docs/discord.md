# Discord (read-only source)

The `discord` capability lets an agent read the Meshtastic Discord server — channel list,
message history, threads, and a bounded client-side search — so "has anyone hit this on
Discord?" is a tool call, not a copy-paste. It is strictly **read-only**: the bot holds
`View Channels` + `Read Message History` only (permission integer `66560`), and the client
never calls a write endpoint.

Tools: `discord_status` · `discord_channels` · `discord_read` · `discord_search` ·
`discord_thread`. All are `readOnlyHint` + `openWorldHint` (public, user-authored content —
see [Security](#security)).

## Setup for a new operator (≈5 min)

You need a **bot token** whose bot is a member of the Meshtastic guild. Pick one:

**A. Shared org bot (recommended for org leads).** One Discord application, owned by the
Discord *Developer Team* the leads belong to; the bot is already invited, so no server-admin
action is needed. The Developer Portal shows a token **once** — there is no "copy it later",
only *Reset Token*, which invalidates the token everyone else is running. So **do not reset
it from the portal**: ask the team owner for the current token through a password manager
or other secret channel (never Discord DM, never an issue). Team membership exists for
ownership continuity and the ability to reset a leaked token, not as a token-distribution
mechanism. Ask the owner to add you at <https://discord.com/developers/teams>.

**B. Your own bot.** Create an application at <https://discord.com/developers/applications>,
open **Bot** → *Reset Token*, turn **Public Bot** off, enable **Message Content Intent**
(without it every message reads back as empty content). Then a server admin must invite it:

```text
https://discord.com/oauth2/authorize?client_id=<YOUR_APP_ID>&scope=bot&permissions=66560
```

Either way, install the token where the MCP looks for it:

```bash
meshtastic-mcp doctor | grep -A1 '\[discord\]'     # ✗ discord … save a read-only bot token to <path>
TOKEN_FILE=<path from doctor>                      # Linux: ~/.config/meshtastic-mcp/discord.token
mkdir -p "$(dirname "$TOKEN_FILE")"                #  macOS: ~/Library/Application Support/meshtastic-mcp/discord.token
printf '%s' '<token>' > "$TOKEN_FILE" && chmod 600 "$TOKEN_FILE"
meshtastic-mcp doctor | grep -A1 '\[discord\]'     # ✓ discord ok  <path> → Meshtastic
```

The path is `platformdirs.user_config_dir("meshtastic-mcp")/discord.token` — `doctor` prints
the resolved one for your platform. `$DISCORD_BOT_TOKEN` overrides the file (useful for CI /
MCP-client `env` blocks). If the bot is in more than one guild, set `$DISCORD_GUILD_ID`.

Restart the MCP server — capability detection runs at startup and the `discord_*` tools are
only advertised when a token resolves.

## Usage

```python
discord_channels()                                   # names/ids/categories; cached per process
discord_read(channel="#android", limit=50)           # newest first; page with before=<last id>
discord_search("ble stale", channel="android")       # case-insensitive; regex=True for patterns
discord_search("OTAFIX", max_scan=2000)              # every text channel, deeper horizon
discord_thread("ble-thread")                         # whole thread, oldest first
```

Search is **client-side**: Discord's search endpoint is user-account only, so the client
pages newest-first through at most `max_scan` messages per channel and filters locally.
The result reports `scanned`, `channels_scanned` and `oldest_seen` — the horizon it actually
covered — so a miss means "not in the last N", not "never said". Raise `max_scan` to look
further back; one REST call per 100 messages, 429s are honoured with one retry.

## Security

Every message is untrusted, user-authored text from a public community server — the same
posture as `packets_window` and `cot_relay_status`. The tools are `openWorldHint` so clients
can see untrusted content entering the session. Do not process Discord content and call
`send_text` (or any other exfiltration path) in the same agentic task without human review.

The token is a credential for a read-only identity, but treat it like one: never commit it,
never paste it into an issue, `chmod 600` the file. `discord_status` and `doctor` report the
token *source*, never the token. If it leaks, *Reset Token* in the Developer Portal — the old
one dies instantly.

The bot is deliberately not a gateway (websocket) client: no presence, no event stream, no
`discord.py` dependency — just stdlib `urllib` against the REST API, so nothing here can
ever react to or post in the server.
