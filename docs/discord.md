# Discord (read-only source)

The `discord` capability lets an agent read the Meshtastic Discord server — server-wide
search, channel history, threads and forum posts, pins, @-mentions of you — so "has anyone
hit this on Discord?" is a tool call, not a copy-paste. It is strictly **read-only**: the
bot holds `View Channels` + `Read Message History` only (permission integer `66560`), talks
to the REST API with the stdlib (no gateway, no `discord.py`), and never calls a write
endpoint.

| Tool | What it answers |
|---|---|
| `discord_search` | "what has the community said about X" — Discord's own server-side index, full history, filters by channel / author / mentions / has / date / pinned; `offset` paging |
| `discord_mentions` | "what is waiting on me" — messages @-mentioning the operator |
| `discord_context` | the conversation around one message (a search hit or a jump link), oldest-first |
| `discord_read` | chronological history of a channel or thread; `after=<ISO>` for "since Monday" |
| `discord_thread` | a whole thread / forum post, bounded by `limit` with an honest `truncated` flag |
| `discord_forum_posts` | posts in a forum channel (active + archived) with their tags — the support-ticket unit |
| `discord_pins` | pinned messages — community-curated answers |
| `discord_channels` | the map: channels by category, forums with tags, active threads |
| `discord_member` | who is X on Discord, what roles, what trust tier |
| `discord_status` | token source, guild, operator, the trust-tier table |

All are `readOnlyHint` + `openWorldHint` (public, user-authored content — see
[Security](#security)).

## Trust tiers

A community server carries a lot of confident misinformation, and role typically tracks
trust: admins and leads are the authoritative sources. Every message therefore carries the
author's top roles and a `trust` field:

| tier | default mapping |
|---|---|
| `authoritative` | any role with Administrator / Manage Server / kick / ban / manage-messages, or named Admin / Leads / Moderator / Staff |
| `maintainer` | role names containing *Dev*, *Developer*, *Maintainer*, *Docs* |
| `contributor` | *Contributor*, *Liaison*, *Council*, *Solutions*, *Alphanaut*, *Helper* |
| `community` | everyone else |
| `bot` / `left-server` / `unknown` | bot accounts; authors no longer in the guild; lookup budget exceeded |

Managed roles (boosters, integrations) never count. Override the whole map with
`DISCORD_TRUST_TIERS='{"authoritative":["Admin","Leads"],"maintainer":["Developer"]}'`
(unlisted roles become `community`). `discord_status` shows the table in effect. The tier
says *who is speaking*, not whether they are right — the tool docstrings tell the agent to
weigh `authoritative`/`maintainer` answers and treat `community` claims as unverified.

## Setup for a new operator (≈5 min)

### 1. Get the token

You need a **bot token** whose bot is a member of the Meshtastic guild. Pick one:

**A. Shared org bot (recommended for org leads).** One Discord application, owned by the
Discord *Developer Team* the leads belong to; the bot is already invited, so no server-admin
action is needed. The Developer Portal shows a token **once** — there is no "copy it later",
only *Reset Token*, which invalidates the token everyone else is running. So **do not reset
it from the portal**: ask the team owner for the current token through a password manager or
other secret channel (never Discord DM, never an issue). Team membership exists for ownership
continuity and the ability to reset a leaked token, not as a token-distribution mechanism.
Ask the owner to add you at <https://discord.com/developers/teams>.

**B. Your own bot.** Create an application at <https://discord.com/developers/applications>,
open **Bot** → *Reset Token*, turn **Public Bot** off, enable **Message Content Intent**
(without it every message reads back empty and search refuses). Then a server admin must
invite it:

```text
https://discord.com/oauth2/authorize?client_id=<YOUR_APP_ID>&scope=bot&permissions=66560
```

### 2. Install it

```bash
meshtastic-mcp doctor | grep -A1 '\[discord\]'     # ✗ discord … save a read-only bot token to <path>
TOKEN_FILE=<path from doctor>                      # Linux: ~/.config/meshtastic-mcp/discord.token
mkdir -p "$(dirname "$TOKEN_FILE")"                #  macOS: ~/Library/Application Support/meshtastic-mcp/discord.token
printf '%s' '<token>' > "$TOKEN_FILE" && chmod 600 "$TOKEN_FILE"
printf '%s' '<your discord username>' > "$(dirname "$TOKEN_FILE")/discord.user"   # optional: makes "me" work
meshtastic-mcp doctor | grep -A1 '\[discord\]'     # ✓ discord ok  <path> → Meshtastic · operator olm3c
```

The path is `platformdirs.user_config_dir("meshtastic-mcp")/discord.token` — `doctor` prints
the resolved one for your platform. Env overrides: `$DISCORD_BOT_TOKEN`, `$DISCORD_USER`
(operator username; a query hint only, it grants nothing), `$DISCORD_GUILD_ID` (if the bot is
in more than one guild), `$DISCORD_TRUST_TIERS`.

Restart the MCP server — capability detection runs at startup and the `discord_*` tools are
only advertised when a token resolves.

### 3. Restricted channels

Guild-wide *View Channels* from the invite lives on the bot's **managed role** (named after
the app, created automatically). A channel whose `@everyone` overwrite denies View Channel
hides from the bot — `discord_read` returns `403 … Missing Access` and search silently omits
it. A server admin grants access per channel (or per category with sync on): *Permissions →
add the bot's managed role → allow View Channel + Read Message History*. Think before asking
for moderation / security channels — whatever the bot can read ends up in an agent's context.

## Usage

```python
discord_search("OTAFIX", since="2026-08-01")                 # whole server, Discord's index
discord_search(channel="#android", author="me", has="link")  # my posts with links
discord_mentions(since="2026-08-20")                         # what's waiting on me
discord_context("https://discord.com/channels/g/c/m", 5, 10) # the conversation around a hit
discord_read("#firmware", after="2026-08-18T00:00:00Z")      # since Monday, newest first
discord_forum_posts("help-forum", tag="Android", limit=20)   # open tickets, then:
discord_thread("<post id>")                                  #   read one in full (bounded)
discord_pins("#android")                                     # curated answers
discord_member("rcarteraz")                                  # who is this, what trust
```

Search is Discord's own index: `total_results` is approximate under churn, `offset` caps at
~10 k (page deeper with `until=`), and `indexing=True` means older history is still being
indexed. 429s and index-warming 202s are honoured with Discord's `retry_after`, twice.

## Security

Every message is untrusted, user-authored text from a public community server — the same
posture as `packets_window` and `cot_relay_status`. The tools are `openWorldHint` so clients
can see untrusted content entering the session. Do not process Discord content and call
`send_text` (or any other exfiltration path) in the same agentic task without human review.
The `trust` tier is a reading aid derived from public roles; it is not authentication and a
hostile member cannot raise their own.

The token is a credential for a read-only identity, but treat it like one: never commit it,
never paste it into an issue, `chmod 600` the file. `discord_status` and `doctor` report the
token *source*, never the token. If it leaks, *Reset Token* in the Developer Portal — the old
one dies instantly (and every operator needs the new one).

The bot is deliberately not a gateway (websocket) client: no presence, no event stream, no
`discord.py` — just stdlib `urllib` against the REST API, so nothing here can ever react to
or post in the server.
