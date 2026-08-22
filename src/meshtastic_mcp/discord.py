# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only Discord bridge (the ``discord`` capability).

Feeds the Meshtastic Discord server into the MCP as a *source* — channels,
message history, threads — so an agent can answer "has anyone hit this on
Discord?" without a human copy-pasting. Strictly read-only: the bot is invited
with ``View Channels`` + ``Read Message History`` (permissions ``66560``) and
nothing here calls a write endpoint. The Discord REST API is driven with the
stdlib (``urllib``) so the capability adds no dependency to core.

Gating: the capability is active when a bot token resolves —
``$DISCORD_BOT_TOKEN`` or the file ``<user-config-dir>/meshtastic-mcp/discord.token``
(``~/.config/meshtastic-mcp/discord.token`` on Linux; see :func:`token_path`).
No network call happens at startup. The bot identity is whatever the token
belongs to; each operator either shares one app via a Discord Developer Team
or registers their own and has a server admin invite it — see ``docs/discord.md``.

Everything returned by these tools is **untrusted, user-authored content**
from a public community server — the same posture as ``packets_window`` /
``cot_relay_status``. The tools are ``openWorldHint`` for that reason.
Message search is client-side: Discord's search endpoint is user-account only,
so ``search`` pages through recent history and filters locally (bounded by
``max_scan``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"
GUILD_ENV = "DISCORD_GUILD_ID"
API = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/meshtastic/meshtastic-mcp, read-only)"
# Discord's hard cap per GET /channels/{id}/messages.
PAGE_MAX = 100
# Text-bearing channel types: GUILD_TEXT, GUILD_ANNOUNCEMENT, and the three thread kinds.
# GUILD_FORUM (15) holds only threads; its posts surface via `threads`.
_TEXT_TYPES = {0: "text", 5: "announcement", 10: "thread", 11: "thread", 12: "thread"}
_CATEGORY = 4
_FORUM = 15


class DiscordError(RuntimeError):
    """Raised when the token is missing or Discord returns an unusable result."""


# ---------- token / guild resolution ---------------------------------------


def token_path() -> Path:
    """Where the token file lives when ``$DISCORD_BOT_TOKEN`` is unset."""
    from platformdirs import user_config_dir

    return Path(user_config_dir("meshtastic-mcp")) / "discord.token"


def token_or_none() -> str | None:
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    p = token_path()
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tok or None


def available() -> bool:
    """True when a bot token resolves (no network probe)."""
    return token_or_none() is not None


def token_source() -> str:
    """Human-readable description of where the token came from (never the token)."""
    if os.environ.get(TOKEN_ENV, "").strip():
        return f"${TOKEN_ENV}"
    return str(token_path())


# ---------- transport --------------------------------------------------------

# (method, path, query) -> parsed JSON. Swappable for tests.
Transport = Callable[[str, str, dict[str, Any] | None], Any]


def _http(method: str, path: str, query: dict[str, Any] | None = None) -> Any:
    tok = token_or_none()
    if tok is None:
        raise DiscordError(f"no Discord bot token (${TOKEN_ENV} or {token_path()})")
    url = f"{API}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bot {tok}", "User-Agent": _USER_AGENT},
    )
    # One polite retry on 429 — the bot is read-only and low-volume, so a single
    # wait almost always clears it; anything worse is surfaced to the caller.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 and attempt == 0:
                try:
                    delay = float(json.loads(body).get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    delay = 1.0
                time.sleep(min(delay, 10.0))
                continue
            hint = ""
            if exc.code == 401:
                hint = " (token rejected — reset it in the Discord Developer Portal)"
            elif exc.code == 403:
                hint = " (bot lacks View Channels / Read Message History here)"
            raise DiscordError(
                f"discord {method} {path} -> {exc.code}{hint}: {body[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise DiscordError(f"discord {method} {path} unreachable: {exc.reason}") from exc
    raise DiscordError(f"discord {method} {path}: rate-limited twice")  # pragma: no cover


class Client:
    """Thin read-only REST client. ``transport`` is injectable for tests."""

    def __init__(self, transport: Transport | None = None, guild_id: str | None = None):
        self._t: Transport = transport or _http
        self._guild = guild_id or os.environ.get(GUILD_ENV, "").strip() or None
        self._channels: list[dict[str, Any]] | None = None

    # -- guild ---------------------------------------------------------------

    def guilds(self) -> list[dict[str, Any]]:
        return [
            {"id": g["id"], "name": g.get("name", "")}
            for g in self._t("GET", "/users/@me/guilds", None) or []
        ]

    def guild_id(self) -> str:
        """``$DISCORD_GUILD_ID``, else the only guild the bot is in."""
        if self._guild:
            return self._guild
        gs = self.guilds()
        if len(gs) == 1:
            self._guild = gs[0]["id"]
            return self._guild
        if not gs:
            raise DiscordError("bot is in no guild — a server admin must invite it")
        names = ", ".join(f"{g['name']}={g['id']}" for g in gs)
        raise DiscordError(f"bot is in several guilds; set ${GUILD_ENV} to one of: {names}")

    # -- channels ------------------------------------------------------------

    def channels(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Text-bearing channels + active threads, flattened with their category."""
        if self._channels is not None and not refresh:
            return self._channels
        gid = self.guild_id()
        raw = self._t("GET", f"/guilds/{gid}/channels", None) or []
        cats = {c["id"]: c.get("name", "") for c in raw if c.get("type") == _CATEGORY}
        out: list[dict[str, Any]] = []
        for c in raw:
            kind = _TEXT_TYPES.get(c.get("type", -1))
            if c.get("type") == _FORUM:
                kind = "forum"
            if kind is None:
                continue
            out.append(
                {
                    "id": c["id"],
                    "name": c.get("name", ""),
                    "kind": kind,
                    "category": cats.get(c.get("parent_id") or "", ""),
                    "topic": (c.get("topic") or "")[:200],
                }
            )
        active = self._t("GET", f"/guilds/{gid}/threads/active", None) or {}
        by_id = {c["id"]: c for c in out}
        for t in active.get("threads", []):
            parent = by_id.get(t.get("parent_id") or "")
            out.append(
                {
                    "id": t["id"],
                    "name": t.get("name", ""),
                    "kind": "thread",
                    "category": parent["name"] if parent else "",
                    "topic": "",
                }
            )
        self._channels = out
        return out

    def resolve_channel(self, ref: str) -> str:
        """Accept a snowflake id, ``#name``, or a bare name (case-insensitive)."""
        ref = ref.strip().lstrip("#")
        if ref.isdigit():
            return ref
        low = ref.lower()
        hits = [c for c in self.channels() if c["name"].lower() == low]
        if not hits:
            # one refresh in case the channel/thread is newer than the cache
            hits = [c for c in self.channels(refresh=True) if c["name"].lower() == low]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            raise DiscordError(f"no channel named {ref!r} (see discord_channels)")
        ids = ", ".join(f"{c['id']} [{c['category']}]" for c in hits)
        raise DiscordError(f"{len(hits)} channels named {ref!r}; use an id: {ids}")

    # -- messages ------------------------------------------------------------

    def messages(
        self,
        channel: str,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first page of messages (``limit`` ≤ 100)."""
        cid = self.resolve_channel(channel)
        q: dict[str, Any] = {"limit": max(1, min(int(limit), PAGE_MAX))}
        if before:
            q["before"] = before
        if after:
            q["after"] = after
        raw = self._t("GET", f"/channels/{cid}/messages", q) or []
        return [_slim(m) for m in raw]

    def search(
        self,
        query: str,
        channel: str | None = None,
        limit: int = 20,
        max_scan: int = 500,
        regex: bool = False,
    ) -> dict[str, Any]:
        """Client-side scan of recent history for ``query``.

        Scans newest-first, at most ``max_scan`` messages per channel (all
        text channels when ``channel`` is None). Returns matches + how much
        was scanned so the caller can see the horizon it actually covered.
        """
        pat = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        targets = (
            [self.resolve_channel(channel)]
            if channel
            else [c["id"] for c in self.channels() if c["kind"] != "forum"]
        )
        names = {c["id"]: c["name"] for c in self.channels()}
        matches: list[dict[str, Any]] = []
        scanned = 0
        oldest: str | None = None
        for cid in targets:
            before: str | None = None
            seen = 0
            while seen < max_scan and len(matches) < limit:
                page = self.messages(cid, limit=PAGE_MAX, before=before)
                if not page:
                    break
                for m in page:
                    seen += 1
                    if pat.search(m["content"]):
                        matches.append({**m, "channel": names.get(cid, cid), "channel_id": cid})
                        if len(matches) >= limit:
                            break
                before = page[-1]["id"]
                oldest = _older(oldest, page[-1]["timestamp"])
            scanned += seen
            if len(matches) >= limit:
                break
        return {
            "query": query,
            "matches": matches,
            "scanned": scanned,
            "channels_scanned": len(targets),
            "oldest_seen": oldest,
            "truncated": len(matches) >= limit,
        }

    def thread(self, thread: str, limit: int = 100) -> dict[str, Any]:
        """A thread's metadata + its messages oldest-first (the readable order)."""
        tid = self.resolve_channel(thread)
        meta = self._t("GET", f"/channels/{tid}", None) or {}
        msgs: list[dict[str, Any]] = []
        before: str | None = None
        while len(msgs) < limit:
            page = self.messages(tid, limit=min(PAGE_MAX, limit - len(msgs)), before=before)
            if not page:
                break
            msgs.extend(page)
            before = page[-1]["id"]
        msgs.reverse()
        return {
            "id": tid,
            "name": meta.get("name", ""),
            "parent_id": meta.get("parent_id"),
            "archived": bool((meta.get("thread_metadata") or {}).get("archived", False)),
            "message_count": meta.get("message_count"),
            "messages": msgs,
        }


# ---------- helpers ----------------------------------------------------------


def _slim(m: dict[str, Any]) -> dict[str, Any]:
    """Keep what an agent needs; drop avatars, flags, embeds' binary noise."""
    a = m.get("author") or {}
    ref = m.get("message_reference") or {}
    return {
        "id": m.get("id", ""),
        "timestamp": m.get("timestamp", ""),
        "author": a.get("global_name") or a.get("username", ""),
        "author_id": a.get("id", ""),
        "bot": bool(a.get("bot", False)),
        "content": m.get("content", ""),
        "reply_to": ref.get("message_id"),
        "attachments": [x.get("filename", "") for x in m.get("attachments") or []],
        "reactions": sum(int(r.get("count", 0)) for r in m.get("reactions") or []),
        "thread_id": (m.get("thread") or {}).get("id"),
    }


def _older(a: str | None, b: str) -> str:
    return b if a is None or b < a else a


def status(client: Client | None = None) -> dict[str, Any]:
    """Capability report without touching message content."""
    if not available():
        return {"ok": False, "error": f"no token (${TOKEN_ENV} or {token_path()})"}
    c = client or Client()
    try:
        gs = c.guilds()
    except DiscordError as exc:
        return {"ok": False, "token_source": token_source(), "error": str(exc)}
    return {"ok": True, "token_source": token_source(), "guilds": gs, "read_only": True}
