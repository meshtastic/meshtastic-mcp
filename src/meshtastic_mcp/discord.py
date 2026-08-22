# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only Discord bridge (the ``discord`` capability).

Feeds the Meshtastic Discord server into the MCP as a *source* — guild search,
channel history, threads, forum posts, pins, mentions — so an agent can answer
"has anyone hit this on Discord?" without a human copy-pasting. Strictly
read-only: the bot is invited with ``View Channels`` + ``Read Message History``
(permissions ``66560``) and nothing here calls a write endpoint. The REST API is
driven with the stdlib (``urllib``); no gateway connection, no new dependency.

Gating: active when a bot token resolves — ``$DISCORD_BOT_TOKEN`` or
``<user-config-dir>/meshtastic-mcp/discord.token`` (:func:`token_path`). No
network call at startup. Optional operator identity (``$DISCORD_USER`` or the
sibling ``discord.user`` file) lets ``author="me"`` / ``mentions="me"`` resolve;
it is a query hint only, it grants nothing.

Trust: community servers carry a lot of confident misinformation. Every message
is annotated with the author's top roles and a ``trust`` tier derived from them
(``authoritative`` admins/leads/mods → ``maintainer`` → ``contributor`` →
``community`` → ``bot``). Override the mapping with ``$DISCORD_TRUST_TIERS``
(JSON ``{tier: [role name, ...]}``). The tier is a reading aid for the agent —
it says who is speaking, not whether they are right.

Everything returned is **untrusted, user-authored content** from a public
server — same posture as ``packets_window`` / ``cot_relay_status``; the tools
are ``openWorldHint``. See ``docs/discord.md``.
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
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"
GUILD_ENV = "DISCORD_GUILD_ID"
USER_ENV = "DISCORD_USER"
TRUST_ENV = "DISCORD_TRUST_TIERS"
API = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/meshtastic/meshtastic-mcp, read-only)"
PAGE_MAX = 100  # GET /channels/{id}/messages hard cap
SEARCH_MAX = 25  # GET /guilds/{id}/messages/search hard cap
SEARCH_OFFSET_MAX = 9975
_DISCORD_EPOCH_MS = 1420070400000
# Channel types we surface. GUILD_FORUM/GUILD_MEDIA hold only threads (posts).
_KINDS = {
    0: "text",
    5: "announcement",
    10: "thread",
    11: "thread",
    12: "thread",
    15: "forum",
    16: "forum",
}
_CATEGORY = 4
_ADMIN_PERM = 0x8
_MANAGE_GUILD = 0x20
_MOD_PERMS = 0x2 | 0x4 | 0x2000  # kick | ban | manage messages
TRUST_TIERS = ("authoritative", "maintainer", "contributor", "community", "bot")
_DEFAULT_TIER_PATTERNS = {
    "authoritative": re.compile(r"^(admin|administrator|leads?|moderators?|mods?|staff)$", re.I),
    "maintainer": re.compile(r"dev\b|developer|maintainer|docs", re.I),
    "contributor": re.compile(r"contributor|liaison|council|solutions|alphanaut|helper", re.I),
}
_JUMP_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


class DiscordError(RuntimeError):
    """Raised when the token is missing or Discord returns an unusable result."""


# ---------- config: token / guild / operator --------------------------------


def _config_dir() -> Path:
    from platformdirs import user_config_dir

    return Path(user_config_dir("meshtastic-mcp"))


def token_path() -> Path:
    """Where the token file lives when ``$DISCORD_BOT_TOKEN`` is unset."""
    return _config_dir() / "discord.token"


def user_path() -> Path:
    """Operator identity file (a Discord username) when ``$DISCORD_USER`` is unset."""
    return _config_dir() / "discord.user"


def _read_secret(env: str, path: Path) -> str | None:
    val = os.environ.get(env, "").strip()
    if val:
        return val
    try:
        val = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return val or None


def token_or_none() -> str | None:
    return _read_secret(TOKEN_ENV, token_path())


def operator_or_none() -> str | None:
    """Configured operator username (``olm3c``), or None."""
    return _read_secret(USER_ENV, user_path())


def available() -> bool:
    """True when a bot token resolves (no network probe)."""
    return token_or_none() is not None


def token_source() -> str:
    """Where the token came from (never the token itself)."""
    if os.environ.get(TOKEN_ENV, "").strip():
        return f"${TOKEN_ENV}"
    return str(token_path())


def trust_overrides() -> dict[str, list[str]]:
    raw = os.environ.get(TRUST_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise DiscordError(f"${TRUST_ENV} is not JSON: {exc}") from exc
    bad = set(data) - set(TRUST_TIERS)
    if bad:
        raise DiscordError(f"${TRUST_ENV}: unknown tiers {sorted(bad)}; use {TRUST_TIERS}")
    return {k: [str(x) for x in v] for k, v in data.items()}


# ---------- snowflakes / links ------------------------------------------------


def snowflake_to_iso(snowflake: str) -> str:
    ms = (int(snowflake) >> 22) + _DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def to_snowflake(when: str | None) -> str | None:
    """ISO-8601 timestamp (or a bare snowflake) → snowflake string, for cursor params."""
    if not when:
        return None
    s = when.strip()
    if s.isdigit():
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscordError(f"not an ISO-8601 timestamp or snowflake: {when!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ms = int(dt.timestamp() * 1000) - _DISCORD_EPOCH_MS
    if ms < 0:
        raise DiscordError(f"{when!r} predates Discord (2015)")
    return str(ms << 22)


def parse_jump_link(url: str) -> tuple[str, str, str]:
    """``https://discord.com/channels/{guild}/{channel}/{message}`` → ids."""
    m = _JUMP_RE.search(url)
    if not m:
        raise DiscordError(f"not a Discord message link: {url!r}")
    return m.group(1), m.group(2), m.group(3)


# ---------- transport ---------------------------------------------------------

# (method, path, query) -> parsed JSON. Swappable for tests.
Transport = Callable[[str, str, dict[str, Any] | None], Any]


def _encode_query(query: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for k, v in query.items():
        if v is None:
            continue
        if isinstance(v, list | tuple):
            pairs.extend((k, str(x)) for x in v)
        elif isinstance(v, bool):
            pairs.append((k, "true" if v else "false"))
        else:
            pairs.append((k, str(v)))
    return urllib.parse.urlencode(pairs)


def _http(method: str, path: str, query: dict[str, Any] | None = None) -> Any:
    tok = token_or_none()
    if tok is None:
        raise DiscordError(f"no Discord bot token (${TOKEN_ENV} or {token_path()})")
    url = f"{API}{path}"
    if query:
        qs = _encode_query(query)
        if qs:
            url += "?" + qs
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bot {tok}", "User-Agent": _USER_AGENT}
    )
    # Retries: 429 (rate limit) and 202/110000 (search index warming), each with
    # Discord's own retry_after. Read-only + low volume, so one or two waits almost
    # always clear it; anything worse is surfaced to the caller.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 and attempt < 2:
                time.sleep(min(_retry_after(body), 10.0))
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
        try:
            data = json.loads(raw) if raw else None
        except ValueError as exc:
            raise DiscordError(f"discord {method} {path}: malformed JSON ({exc})") from exc
        if status == 202 and isinstance(data, dict) and data.get("code") == 110000 and attempt < 2:
            time.sleep(min(float(data.get("retry_after", 1.0)), 10.0))
            continue
        return data
    raise DiscordError(f"discord {method} {path}: still rate-limited / indexing after retries")


def _retry_after(body: str) -> float:
    try:
        return float(json.loads(body).get("retry_after", 1.0))
    except (ValueError, AttributeError, TypeError):
        return 1.0


# ---------- client ------------------------------------------------------------


class Client:
    """Thin read-only REST client. ``transport`` is injectable for tests."""

    def __init__(self, transport: Transport | None = None, guild_id: str | None = None):
        self._t: Transport = transport or _http
        self._guild = guild_id or os.environ.get(GUILD_ENV, "").strip() or None
        self._channels: list[dict[str, Any]] | None = None
        self._roles: dict[str, dict[str, Any]] | None = None
        self._members: dict[str, dict[str, Any]] = {}
        self._operator: dict[str, Any] | None = None
        self._tiers: dict[str, str] | None = None  # role id -> tier
        self._tags: dict[str, dict[str, str]] = {}  # forum id -> tag id -> name

    # -- guild -----------------------------------------------------------------

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

    def guild_info(self) -> dict[str, Any]:
        g = self._t("GET", f"/guilds/{self.guild_id()}", {"with_counts": True}) or {}
        return {
            "id": g.get("id"),
            "name": g.get("name", ""),
            "description": g.get("description"),
            "members": g.get("approximate_member_count"),
            "online": g.get("approximate_presence_count"),
            "features": g.get("features", []),
        }

    # -- roles / trust ---------------------------------------------------------

    def roles(self) -> dict[str, dict[str, Any]]:
        """Role id → {name, position, managed, perms} (cached)."""
        if self._roles is None:
            raw = self._t("GET", f"/guilds/{self.guild_id()}/roles", None) or []
            self._roles = {
                r["id"]: {
                    "name": r.get("name", ""),
                    "position": int(r.get("position", 0)),
                    "managed": bool(r.get("managed", False)),
                    "perms": int(r.get("permissions", 0) or 0),
                }
                for r in raw
            }
        return self._roles

    def trust_map(self) -> dict[str, str]:
        """Role id → tier. ``$DISCORD_TRUST_TIERS`` names win; else name/permission heuristics."""
        if self._tiers is not None:
            return self._tiers
        override = trust_overrides()
        by_name = {n.lower(): t for t, names in override.items() for n in names}
        out: dict[str, str] = {}
        for rid, r in self.roles().items():
            name = r["name"]
            if name == "@everyone":
                continue
            if name.lower() in by_name:
                out[rid] = by_name[name.lower()]
                continue
            if override:
                continue  # explicit map given: unlisted roles are community
            if r["managed"]:
                continue  # bot/booster/integration roles say nothing about trust
            if r["perms"] & (_ADMIN_PERM | _MANAGE_GUILD | _MOD_PERMS) or _DEFAULT_TIER_PATTERNS[
                "authoritative"
            ].match(name):
                out[rid] = "authoritative"
            elif _DEFAULT_TIER_PATTERNS["maintainer"].search(name):
                out[rid] = "maintainer"
            elif _DEFAULT_TIER_PATTERNS["contributor"].search(name):
                out[rid] = "contributor"
        self._tiers = out
        return out

    def trust_table(self) -> dict[str, list[str]]:
        """Tier → role names (highest position first), for ``discord_status`` / docs."""
        roles = self.roles()
        table: dict[str, list[str]] = {t: [] for t in TRUST_TIERS[:3]}
        for rid, tier in sorted(self.trust_map().items(), key=lambda kv: -roles[kv[0]]["position"]):
            table[tier].append(roles[rid]["name"])
        return table

    def _tier_for(self, role_ids: Iterable[str], is_bot: bool) -> str:
        if is_bot:
            return "bot"
        tiers = self.trust_map()
        best = TRUST_TIERS.index("community")
        for rid in role_ids:
            t = tiers.get(rid)
            if t is not None:
                best = min(best, TRUST_TIERS.index(t))
        return TRUST_TIERS[best]

    # -- members ---------------------------------------------------------------

    def member(self, user_id: str) -> dict[str, Any] | None:
        """Guild member (cached). ``None`` when the user left the server."""
        if user_id in self._members:
            return self._members[user_id] or None
        try:
            raw = self._t("GET", f"/guilds/{self.guild_id()}/members/{user_id}", None) or {}
        except DiscordError as exc:
            if "-> 404" not in str(exc):
                raise
            self._members[user_id] = {}
            return None
        self._members[user_id] = self._slim_member(raw)
        return self._members[user_id]

    def find_member(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Prefix search on username / nickname (no privileged intent needed)."""
        raw = self._t(
            "GET",
            f"/guilds/{self.guild_id()}/members/search",
            {"query": query, "limit": max(1, min(int(limit), 100))},
        )
        out = [self._slim_member(m) for m in raw or []]
        for m in out:
            self._members[m["id"]] = m
        return out

    def _slim_member(self, m: dict[str, Any]) -> dict[str, Any]:
        u = m.get("user") or {}
        roles = self.roles()
        rids = sorted(
            (r for r in m.get("roles") or [] if r in roles), key=lambda r: -roles[r]["position"]
        )
        return {
            "id": u.get("id", ""),
            "username": u.get("username", ""),
            "display": m.get("nick") or u.get("global_name") or u.get("username", ""),
            "bot": bool(u.get("bot", False)),
            "roles": [roles[r]["name"] for r in rids],
            "trust": self._tier_for(rids, bool(u.get("bot", False))),
            "joined_at": m.get("joined_at"),
        }

    def operator(self) -> dict[str, Any] | None:
        """The configured operator resolved to a member, or None when unset."""
        if self._operator is not None:
            return self._operator or None
        name = operator_or_none()
        if not name:
            self._operator = {}
            return None
        hits = [
            m for m in self.find_member(name, limit=20) if m["username"].lower() == name.lower()
        ]
        if not hits:
            raise DiscordError(f"operator {name!r} (${USER_ENV}) is not a member of this guild")
        self._operator = hits[0]
        return self._operator

    def resolve_user(self, ref: str) -> str:
        """``me`` / ``@name`` / ``name`` / snowflake → user id."""
        ref = ref.strip().lstrip("@")
        if ref.lower() == "me":
            op = self.operator()
            if op is None:
                raise DiscordError(
                    f"'me' needs an operator identity: set ${USER_ENV} or {user_path()}"
                )
            return op["id"]
        if ref.isdigit():
            return ref
        hits = [m for m in self.find_member(ref, limit=20) if m["username"].lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            raise DiscordError(f"no member with username {ref!r} (try discord_member)")
        raise DiscordError(f"{len(hits)} members match {ref!r}; use an id")

    # -- channels --------------------------------------------------------------

    def channels(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Text-bearing channels, forums, and active threads, with category and tags."""
        if self._channels is not None and not refresh:
            return self._channels
        gid = self.guild_id()
        raw = self._t("GET", f"/guilds/{gid}/channels", None) or []
        cats = {c["id"]: c.get("name", "") for c in raw if c.get("type") == _CATEGORY}
        out: list[dict[str, Any]] = []
        for c in raw:
            kind = _KINDS.get(c.get("type", -1))
            if kind is None:
                continue
            entry: dict[str, Any] = {
                "id": c["id"],
                "name": c.get("name", ""),
                "kind": kind,
                "category": cats.get(c.get("parent_id") or "", ""),
                "topic": (c.get("topic") or "")[:200],
            }
            if kind == "forum":
                tags = c.get("available_tags") or []
                self._tags[c["id"]] = {t["id"]: t.get("name", "") for t in tags}
                entry["tags"] = [t.get("name", "") for t in tags]
            out.append(entry)
        active = self._t("GET", f"/guilds/{gid}/threads/active", None) or {}
        by_id = {c["id"]: c for c in out}
        for t in active.get("threads", []):
            parent = by_id.get(t.get("parent_id") or "")
            out.append(self._slim_thread(t, parent))
        self._channels = out
        return out

    def _slim_thread(self, t: dict[str, Any], parent: dict[str, Any] | None) -> dict[str, Any]:
        meta = t.get("thread_metadata") or {}
        is_post = parent is not None and parent.get("kind") == "forum"
        tags = (
            self._tag_names(parent["id"], t.get("applied_tags") or []) if parent and is_post else []
        )
        return {
            "id": t["id"],
            "name": t.get("name", ""),
            "kind": "post" if is_post else "thread",
            "category": parent["name"] if parent else "",
            "parent_id": t.get("parent_id"),
            "topic": "",
            "archived": bool(meta.get("archived", False)),
            "locked": bool(meta.get("locked", False)),
            "message_count": t.get("message_count"),
            "created": meta.get("create_timestamp") or snowflake_to_iso(t["id"]),
            "tags": tags,
        }

    def _tag_names(self, forum_id: str, tag_ids: list[str]) -> list[str]:
        if forum_id not in self._tags:
            ch = self._t("GET", f"/channels/{forum_id}", None) or {}
            self._tags[forum_id] = {
                t["id"]: t.get("name", "") for t in ch.get("available_tags") or []
            }
        names = self._tags[forum_id]
        return [names.get(t, t) for t in tag_ids]

    def resolve_channel(self, ref: str) -> str:
        """Snowflake id, ``#name``, bare name (case-insensitive), or a jump link."""
        if "/channels/" in ref:
            return parse_jump_link(ref)[1]
        ref = ref.strip().lstrip("#")
        if ref.isdigit():
            return ref
        low = ref.lower()
        hits = [c for c in self.channels() if c["name"].lower() == low]
        if not hits:
            hits = [c for c in self.channels(refresh=True) if c["name"].lower() == low]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            raise DiscordError(f"no channel named {ref!r} (see discord_channels)")
        ids = ", ".join(f"{c['id']} [{c['category']}]" for c in hits)
        raise DiscordError(f"{len(hits)} channels named {ref!r}; use an id: {ids}")

    def _channel_name(self, cid: str) -> str:
        for c in self._channels or []:
            if c["id"] == cid:
                return c["name"]
        return cid

    # -- messages --------------------------------------------------------------

    def messages(
        self,
        channel: str,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
        around: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page, newest-first. ``before``/``after`` take a snowflake or ISO timestamp."""
        cid = self.resolve_channel(channel)
        if sum(bool(x) for x in (before, after, around)) > 1:
            raise DiscordError("before / after / around are mutually exclusive")
        q: dict[str, Any] = {"limit": max(1, min(int(limit), PAGE_MAX))}
        if before:
            q["before"] = to_snowflake(before)
        if after:
            q["after"] = to_snowflake(after)
        if around:
            q["around"] = around
        raw = self._t("GET", f"/channels/{cid}/messages", q) or []
        return self._enrich([self._slim(m, cid) for m in raw])

    def message(self, channel: str, message_id: str) -> dict[str, Any]:
        cid = self.resolve_channel(channel)
        raw = self._t("GET", f"/channels/{cid}/messages/{message_id}", None) or {}
        return self._enrich([self._slim(raw, cid)])[0]

    def context(
        self, channel: str, message_id: str, before: int = 10, after: int = 10
    ) -> dict[str, Any]:
        """The conversation around one message (e.g. a search hit), oldest-first."""
        cid = self.resolve_channel(channel)
        span = max(1, min(before + after + 1, PAGE_MAX))
        page = self.messages(cid, limit=span, around=message_id)
        page.reverse()
        idx = next((i for i, m in enumerate(page) if m["id"] == message_id), None)
        if idx is not None:
            page = page[max(0, idx - before) : idx + after + 1]
        return {
            "channel": self._channel_name(cid),
            "channel_id": cid,
            "anchor_id": message_id,
            "messages": page,
        }

    def pins(self, channel: str, limit: int = 50) -> list[dict[str, Any]]:
        cid = self.resolve_channel(channel)
        out: list[dict[str, Any]] = []
        before: str | None = None
        while len(out) < limit:
            q: dict[str, Any] = {"limit": max(1, min(50, limit - len(out)))}
            if before:
                q["before"] = before
            page = self._t("GET", f"/channels/{cid}/messages/pins", q) or {}
            items = page.get("items") or []
            for it in items:
                m = self._slim(it.get("message") or {}, cid)
                m["pinned_at"] = it.get("pinned_at")
                out.append(m)
            if not page.get("has_more") or not items:
                break
            before = items[-1].get("pinned_at")
        return self._enrich(out)

    def thread(self, thread: str, limit: int = 100) -> dict[str, Any]:
        """A thread/forum post: metadata + up to ``limit`` messages oldest-first."""
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
        tmeta = meta.get("thread_metadata") or {}
        parent_id = meta.get("parent_id")
        parent = next((c for c in self.channels() if c["id"] == parent_id), None)
        tags = (
            self._tag_names(parent_id, meta.get("applied_tags") or [])
            if parent and parent.get("kind") == "forum" and parent_id
            else []
        )
        count = meta.get("message_count")
        return {
            "id": tid,
            "name": meta.get("name", ""),
            "parent": parent["name"] if parent else parent_id,
            "parent_id": parent_id,
            "archived": bool(tmeta.get("archived", False)),
            "locked": bool(tmeta.get("locked", False)),
            "tags": tags,
            "message_count": count,
            "returned": len(msgs),
            # message_count excludes the starter message, hence the +1.
            "truncated": bool(count is not None and len(msgs) < int(count) + 1),
            "messages": msgs,
        }

    def forum_posts(
        self,
        channel: str,
        include_archived: bool = True,
        tag: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Posts (threads) of a forum channel, newest-first: active + public archived."""
        fid = self.resolve_channel(channel)
        parent = next((c for c in self.channels() if c["id"] == fid), None)
        if parent is None or parent.get("kind") != "forum":
            raise DiscordError(f"{channel!r} is not a forum channel")
        posts = [c for c in self.channels() if c.get("parent_id") == fid]
        if include_archived:
            before: str | None = None
            while len(posts) < limit * 2:  # over-fetch so a tag filter still fills the page
                q: dict[str, Any] = {"limit": 100}
                if before:
                    q["before"] = before
                page = self._t("GET", f"/channels/{fid}/threads/archived/public", q) or {}
                threads = page.get("threads") or []
                posts.extend(self._slim_thread(t, parent) for t in threads)
                if not page.get("has_more") or not threads:
                    break
                before = (threads[-1].get("thread_metadata") or {}).get("archive_timestamp")
        if tag:
            posts = [p for p in posts if tag.lower() in (t.lower() for t in p.get("tags", []))]
        posts.sort(key=lambda p: -int(p["id"]))
        return {
            "forum": parent["name"],
            "forum_id": fid,
            "available_tags": parent.get("tags", []),
            "count": len(posts[:limit]),
            "truncated": len(posts) > limit,
            "posts": posts[:limit],
        }

    # -- search ----------------------------------------------------------------

    def search(
        self,
        query: str | None = None,
        channel: str | list[str] | None = None,
        author: str | None = None,
        mentions: str | None = None,
        has: str | list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        pinned: bool | None = None,
        sort: str = "timestamp",
        limit: int = SEARCH_MAX,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Server-side guild search (Discord's own index). ``author``/``mentions`` accept ``me``."""
        if not any((query, author, mentions, has, pinned)):
            raise DiscordError("search needs at least one of query/author/mentions/has/pinned")
        if sort not in ("timestamp", "relevance"):
            raise DiscordError("sort must be 'timestamp' or 'relevance'")
        if not 0 <= int(offset) <= SEARCH_OFFSET_MAX:
            raise DiscordError(f"offset must be 0..{SEARCH_OFFSET_MAX}")
        q: dict[str, Any] = {
            "limit": max(1, min(int(limit), SEARCH_MAX)),
            "offset": int(offset),
            "sort_by": sort,
            "include_nsfw": False,
        }
        if query:
            q["content"] = query[:1024]
        if channel:
            refs = [channel] if isinstance(channel, str) else list(channel)
            q["channel_id"] = [self.resolve_channel(r) for r in refs]
        if author:
            q["author_id"] = self.resolve_user(author)
        if mentions:
            q["mentions"] = self.resolve_user(mentions)
        if has:
            q["has"] = [has] if isinstance(has, str) else list(has)
        if since:
            q["min_id"] = to_snowflake(since)
        if until:
            q["max_id"] = to_snowflake(until)
        if pinned is not None:
            q["pinned"] = pinned
        raw = self._t("GET", f"/guilds/{self.guild_id()}/messages/search", q) or {}
        hits: list[dict[str, Any]] = []
        for group in raw.get("messages") or []:
            # Legacy context wrapper: each hit arrives as a one-element list.
            for m in group if isinstance(group, list) else [group]:
                hits.append(self._slim(m, m.get("channel_id", "")))
        thread_names = {t["id"]: t.get("name", "") for t in raw.get("threads") or []}
        self.channels()  # warm the name map
        for h in hits:
            h["channel"] = thread_names.get(h["channel_id"]) or self._channel_name(h["channel_id"])
        total = int(raw.get("total_results") or 0)
        nxt = int(offset) + len(hits)
        return {
            "total_results": total,
            "offset": int(offset),
            "next_offset": nxt if hits and nxt < total and nxt <= SEARCH_OFFSET_MAX else None,
            "indexing": bool(raw.get("doing_deep_historical_index", False)),
            "count": len(hits),
            "matches": self._enrich(hits),
        }

    # -- shaping ---------------------------------------------------------------

    def _slim(self, m: dict[str, Any], cid: str) -> dict[str, Any]:
        """Keep what an agent needs; drop avatars, flags, embed binary noise."""
        a = m.get("author") or {}
        ref = m.get("message_reference") or {}
        replied = m.get("referenced_message") or {}
        gid = self._guild or self.guild_id()
        return {
            "id": m.get("id", ""),
            "timestamp": m.get("timestamp", ""),
            "author": a.get("global_name") or a.get("username", ""),
            "author_id": a.get("id", ""),
            "bot": bool(a.get("bot", False)),
            "content": m.get("content", ""),
            "reply_to": ref.get("message_id"),
            "reply_to_author": (replied.get("author") or {}).get("username") if replied else None,
            "attachments": [x.get("filename", "") for x in m.get("attachments") or []],
            "embeds": [e.get("url") or e.get("title", "") for e in m.get("embeds") or []][:5],
            "reactions": {
                (r.get("emoji") or {}).get("name") or "?": int(r.get("count", 0))
                for r in m.get("reactions") or []
            },
            "thread_id": (m.get("thread") or {}).get("id"),
            "channel_id": cid,
            "link": f"https://discord.com/channels/{gid}/{cid}/{m.get('id', '')}",
        }

    def _enrich(self, msgs: list[dict[str, Any]], max_lookups: int = 40) -> list[dict[str, Any]]:
        """Attach ``author_roles`` + ``trust`` per message via cached member lookups."""
        looked = 0
        for m in msgs:
            uid = m.get("author_id")
            if not uid:
                m["author_roles"], m["trust"] = [], "community"
                continue
            if uid not in self._members:
                if looked >= max_lookups:
                    m["author_roles"], m["trust"] = [], "unknown"
                    continue
                looked += 1
                try:
                    self.member(uid)
                except DiscordError as exc:  # keep the read usable; trust is an aid
                    log.debug("member lookup %s failed: %s", uid, exc)
                    self._members[uid] = {}
            mem = self._members.get(uid) or {}
            if not mem:
                m["author_roles"], m["trust"] = [], "bot" if m.get("bot") else "left-server"
            else:
                m["author_roles"] = mem["roles"][:3]
                m["trust"] = mem["trust"]
        return msgs


def status(client: Client | None = None) -> dict[str, Any]:
    """Capability report without touching message content."""
    if not available():
        return {"ok": False, "error": f"no token (${TOKEN_ENV} or {token_path()})"}
    c = client or Client()
    try:
        gs = c.guilds()
    except DiscordError as exc:
        return {"ok": False, "token_source": token_source(), "error": str(exc)}
    rep: dict[str, Any] = {
        "ok": True,
        "token_source": token_source(),
        "guilds": gs,
        "read_only": True,
        "operator": None,
        "trust_tiers": None,
    }
    try:
        rep["operator"] = c.operator()
        rep["trust_tiers"] = c.trust_table()
    except DiscordError as exc:
        rep["warning"] = str(exc)
    return rep
