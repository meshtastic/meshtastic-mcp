# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only Discord bridge: config resolution, trust tiers, search, paging, bounds."""

from __future__ import annotations

from typing import Any

import pytest

from meshtastic_mcp import discord

GUILD = "867578229534359593"
ROLES = [
    {"id": "r-admin", "name": "Admin", "position": 30, "permissions": "8"},
    {"id": "r-leads", "name": "Leads", "position": 29, "permissions": "0"},
    {"id": "r-dev", "name": "Developer", "position": 27, "permissions": "0"},
    {"id": "r-contrib", "name": "Contributor", "position": 22, "permissions": "0"},
    {
        "id": "r-boost",
        "name": "Server Booster",
        "position": 21,
        "permissions": "0",
        "managed": True,
    },
    {"id": "r-mcp", "name": "meshtastic-mcp", "position": 1, "permissions": "0", "managed": True},
    {"id": GUILD, "name": "@everyone", "position": 0, "permissions": "0"},
]
MEMBERS = {
    "u-olm": {"user": {"id": "u-olm", "username": "olm3c"}, "roles": ["r-leads", "r-dev"]},
    "u-dev": {"user": {"id": "u-dev", "username": "devin"}, "roles": ["r-dev"]},
    "u-rando": {
        "user": {"id": "u-rando", "username": "rando", "global_name": "Rando"},
        "roles": [],
    },
    "u-bot": {"user": {"id": "u-bot", "username": "GitHub", "bot": True}, "roles": ["r-boost"]},
}


def _msg(i: int, content: str, author: str = "u-rando", cid: str = "10") -> dict[str, Any]:
    u = dict(MEMBERS[author]["user"])
    return {
        "id": str(1000 - i),
        "channel_id": cid,
        "timestamp": f"2026-08-22T08:{59 - i:02d}:00+00:00",
        "author": u,
        "content": content,
        "attachments": [{"filename": "log.txt"}] if i == 0 else [],
        "reactions": [{"emoji": {"name": "👍"}, "count": 2}] if i == 0 else [],
    }


class FakeTransport:
    """In-memory guild: category, two text channels, a forum with tags, one active thread."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.pages: dict[str, list[dict[str, Any]]] = {
            "10": [
                _msg(i, f"android msg {i}" + (" BLE stale" if i % 3 == 0 else "")) for i in range(7)
            ],
            "11": [_msg(i, f"firmware msg {i}", "u-dev", "11") for i in range(3)],
            "12": [_msg(i, f"thread msg {i}", "u-olm", "12") for i in range(2)],
            "30": [_msg(i, f"post msg {i}", "u-bot", "30") for i in range(4)],
        }
        self.search_hits = [
            _msg(0, "BLE stale on 2.7", "u-dev", "10"),
            _msg(1, "ble thread", "u-olm", "12"),
        ]

    def __call__(self, method: str, path: str, query: dict[str, Any] | None) -> Any:
        self.calls.append((method, path, query))
        assert method == "GET", "read-only bridge must never write"
        q = query or {}
        if path == "/users/@me/guilds":
            return [{"id": GUILD, "name": "Meshtastic"}]
        if path == f"/guilds/{GUILD}/roles":
            return ROLES
        if path == f"/guilds/{GUILD}/members/search":
            return [
                m
                for m in MEMBERS.values()
                if m["user"]["username"].lower().startswith(q["query"].lower())
            ]
        if path.startswith(f"/guilds/{GUILD}/members/"):
            uid = path.rsplit("/", 1)[1]
            if uid not in MEMBERS:
                raise discord.DiscordError(f"discord GET {path} -> 404: unknown member")
            return MEMBERS[uid]
        if path == f"/guilds/{GUILD}/channels":
            return [
                {"id": "1", "type": 4, "name": "Apps"},
                {"id": "10", "type": 0, "name": "android", "parent_id": "1", "topic": "t"},
                {"id": "11", "type": 0, "name": "firmware", "parent_id": None},
                {"id": "13", "type": 2, "name": "voice"},
                {
                    "id": "20",
                    "type": 15,
                    "name": "help-forum",
                    "available_tags": [
                        {"id": "t1", "name": "Android"},
                        {"id": "t2", "name": "Solved"},
                    ],
                },
            ]
        if path == f"/guilds/{GUILD}/threads/active":
            return {
                "threads": [
                    {"id": "12", "name": "ble-thread", "parent_id": "10", "thread_metadata": {}},
                    {
                        "id": "30",
                        "name": "Pairing fails",
                        "parent_id": "20",
                        "applied_tags": ["t1"],
                        "message_count": 3,
                        "thread_metadata": {"archived": False},
                    },
                ]
            }
        if path == "/channels/20/threads/archived/public":
            return {
                "threads": [
                    {
                        "id": "29",
                        "name": "Old post",
                        "parent_id": "20",
                        "applied_tags": ["t1", "t2"],
                        "message_count": 9,
                        "thread_metadata": {
                            "archived": True,
                            "archive_timestamp": "2026-01-01T00:00:00+00:00",
                        },
                    }
                ],
                "has_more": False,
            }
        if path == "/channels/12":
            return {
                "name": "ble-thread",
                "parent_id": "10",
                "thread_metadata": {"archived": False},
                "message_count": 1,
            }
        if path == "/channels/30":
            return {
                "name": "Pairing fails",
                "parent_id": "20",
                "applied_tags": ["t1"],
                "thread_metadata": {"archived": False},
                "message_count": 3,
            }
        if path == "/channels/10/messages/pins":
            return {
                "items": [
                    {
                        "pinned_at": "2026-08-01T00:00:00+00:00",
                        "message": _msg(5, "FAQ: read the docs", "u-olm"),
                    }
                ],
                "has_more": False,
            }
        if path == f"/guilds/{GUILD}/messages/search":
            self.last_search = q
            return {
                "messages": [[m] for m in self.search_hits][: q["limit"]],
                "total_results": 40,
                "threads": [{"id": "12", "name": "ble-thread"}],
            }
        if path.startswith("/channels/") and path.endswith("/messages"):
            cid = path.split("/")[2]
            msgs = self.pages[cid]
            if q.get("around"):
                i = [m["id"] for m in msgs].index(q["around"])
                half = int(q["limit"]) // 2
                return msgs[max(0, i - half) : i + half + 1]
            if q.get("before"):
                ids = [m["id"] for m in msgs]
                msgs = msgs[ids.index(q["before"]) + 1 :]
            if q.get("after"):
                msgs = [m for m in msgs if int(m["id"]) > int(q["after"])]
            return msgs[: int(q.get("limit", 50))]
        raise AssertionError(f"unexpected {path}")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> discord.Client:
    for env in (discord.GUILD_ENV, discord.USER_ENV, discord.TRUST_ENV):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(discord, "user_path", lambda: tmp_path / "no-user")
    return discord.Client(transport=FakeTransport())


# -- config ------------------------------------------------------------------


def test_token_env_wins_over_file(monkeypatch, tmp_path) -> None:
    f = tmp_path / "discord.token"
    f.write_text("file-token\n")
    monkeypatch.setattr(discord, "token_path", lambda: f)
    monkeypatch.delenv(discord.TOKEN_ENV, raising=False)
    assert discord.token_or_none() == "file-token"
    assert discord.token_source() == str(f)
    monkeypatch.setenv(discord.TOKEN_ENV, "env-token")
    assert discord.token_or_none() == "env-token"
    assert discord.token_source() == f"${discord.TOKEN_ENV}"


def test_no_token_means_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(discord, "token_path", lambda: tmp_path / "missing")
    monkeypatch.delenv(discord.TOKEN_ENV, raising=False)
    assert not discord.available()
    assert discord.status()["ok"] is False


def test_guild_id_from_env_skips_probe(monkeypatch) -> None:
    monkeypatch.setenv(discord.GUILD_ENV, "42")
    t = FakeTransport()
    assert discord.Client(transport=t).guild_id() == "42"
    assert t.calls == []


def test_snowflake_roundtrip() -> None:
    sf = discord.to_snowflake("2026-08-22T08:00:00Z")
    assert sf is not None and discord.snowflake_to_iso(sf).startswith("2026-08-22T08:00:00")
    assert discord.to_snowflake("12345") == "12345"
    with pytest.raises(discord.DiscordError, match="ISO-8601"):
        discord.to_snowflake("yesterday")


def test_parse_jump_link() -> None:
    assert discord.parse_jump_link("https://discord.com/channels/1/2/3") == ("1", "2", "3")
    with pytest.raises(discord.DiscordError):
        discord.parse_jump_link("https://example.com/x")


# -- trust -------------------------------------------------------------------


def test_default_trust_tiers_from_roles(client) -> None:
    assert client.trust_table() == {
        "authoritative": ["Admin", "Leads"],
        "maintainer": ["Developer"],
        "contributor": ["Contributor"],
    }


def test_trust_override_replaces_heuristics(monkeypatch) -> None:
    monkeypatch.setenv(discord.TRUST_ENV, '{"authoritative": ["Developer"]}')
    c = discord.Client(transport=FakeTransport())
    assert c.trust_table() == {"authoritative": ["Developer"], "maintainer": [], "contributor": []}
    monkeypatch.setenv(discord.TRUST_ENV, '{"gods": ["Admin"]}')
    with pytest.raises(discord.DiscordError, match="unknown tiers"):
        discord.Client(transport=FakeTransport()).trust_table()


def test_messages_carry_author_trust(client) -> None:
    msgs = client.messages("firmware", limit=1)
    assert msgs[0]["author"] == "devin"
    assert msgs[0]["author_roles"] == ["Developer"] and msgs[0]["trust"] == "maintainer"
    assert client.messages("android", limit=1)[0]["trust"] == "community"
    assert client.messages("Pairing fails", limit=1)[0]["trust"] == "bot"


def test_enrich_caches_member_lookups(client) -> None:
    t = client._t
    client.messages("android", limit=7)  # 7 messages, one author
    lookups = [c for c in t.calls if "/members/u-" in c[1]]
    assert len(lookups) == 1


def test_left_server_author(client) -> None:
    m = client._enrich([{"author_id": "u-gone", "bot": False}])
    assert m[0]["trust"] == "left-server"


# -- operator ----------------------------------------------------------------


def test_operator_resolves_me(monkeypatch, client) -> None:
    monkeypatch.setenv(discord.USER_ENV, "OLM3C")
    monkeypatch.setenv(discord.TOKEN_ENV, "t")  # CI has no token file
    assert client.resolve_user("me") == "u-olm"
    assert discord.status(client)["operator"]["trust"] == "authoritative"


def test_me_without_operator_is_an_error(client) -> None:
    with pytest.raises(discord.DiscordError, match="operator identity"):
        client.resolve_user("me")


def test_resolve_user_by_name_and_id(client) -> None:
    assert client.resolve_user("@devin") == "u-dev"
    assert client.resolve_user("999") == "999"
    with pytest.raises(discord.DiscordError, match="no member"):
        client.resolve_user("nobody")


# -- channels ----------------------------------------------------------------


def test_channels_flatten_category_forum_tags_and_posts(client) -> None:
    by = {c["name"]: c for c in client.channels()}
    assert set(by) == {"android", "firmware", "help-forum", "ble-thread", "Pairing fails"}
    assert by["android"]["category"] == "Apps"
    assert by["help-forum"]["kind"] == "forum" and by["help-forum"]["tags"] == ["Android", "Solved"]
    post = by["Pairing fails"]
    assert (
        post["kind"] == "post" and post["tags"] == ["Android"] and post["category"] == "help-forum"
    )
    assert by["ble-thread"]["kind"] == "thread"


def test_resolve_channel_accepts_hash_name_id_and_link(client) -> None:
    assert client.resolve_channel("#Android") == "10"
    assert client.resolve_channel("10") == "10"
    assert client.resolve_channel(f"https://discord.com/channels/{GUILD}/11/5") == "11"
    with pytest.raises(discord.DiscordError, match="no channel named"):
        client.resolve_channel("nope")


# -- messages ----------------------------------------------------------------


def test_messages_are_slimmed_with_link(client) -> None:
    msgs = client.messages("android", limit=500)
    assert len(msgs) == 7
    first = msgs[0]
    assert first["content"] == "android msg 0 BLE stale"
    assert first["attachments"] == ["log.txt"] and first["reactions"] == {"👍": 2}
    assert first["link"] == f"https://discord.com/channels/{GUILD}/10/1000"
    assert first["author"] == "Rando" and first["author_id"] == "u-rando"


def test_messages_after_accepts_iso(client) -> None:
    sf = discord.to_snowflake("2026-08-22T08:00:00Z")
    assert sf is not None and int(sf) > 1000  # fake ids are tiny → everything is "before"
    assert client.messages("android", after="2026-08-22T08:00:00Z") == []
    with pytest.raises(discord.DiscordError, match="mutually exclusive"):
        client.messages("android", before="1", after="2")


def test_context_is_oldest_first_around_anchor(client) -> None:
    ctx = client.context("android", "997", before=1, after=1)
    assert [m["id"] for m in ctx["messages"]] == ["996", "997", "998"]
    assert ctx["channel"] == "android"


def test_thread_is_bounded_and_honest(client) -> None:
    t = client.thread("Pairing fails", limit=2)
    assert t["parent"] == "help-forum" and t["tags"] == ["Android"]
    assert [m["content"] for m in t["messages"]] == ["post msg 1", "post msg 0"]
    assert t["returned"] == 2 and t["message_count"] == 3 and t["truncated"] is True
    full = client.thread("ble-thread")
    assert full["truncated"] is False


def test_forum_posts_merge_active_and_archived_with_tag_filter(client) -> None:
    r = client.forum_posts("help-forum")
    assert [p["name"] for p in r["posts"]] == ["Pairing fails", "Old post"]
    assert r["available_tags"] == ["Android", "Solved"]
    solved = client.forum_posts("help-forum", tag="solved")
    assert [p["name"] for p in solved["posts"]] == ["Old post"]
    assert solved["posts"][0]["archived"] is True
    with pytest.raises(discord.DiscordError, match="not a forum"):
        client.forum_posts("android")


def test_pins(client) -> None:
    pins = client.pins("android")
    assert len(pins) == 1 and pins[0]["pinned_at"].startswith("2026-08-01")
    assert pins[0]["trust"] == "authoritative"


# -- search ------------------------------------------------------------------


def test_search_builds_server_query_and_shapes_hits(monkeypatch, client) -> None:
    monkeypatch.setenv(discord.USER_ENV, "olm3c")
    r = client.search(
        "ble stale",
        channel=["android", "11"],
        mentions="me",
        since="2026-08-01T00:00:00Z",
        has="link",
        limit=2,
    )
    q = client._t.last_search
    assert q["content"] == "ble stale" and q["channel_id"] == ["10", "11"]
    assert q["mentions"] == "u-olm" and q["has"] == ["link"] and "min_id" in q
    assert q["limit"] == 2 and q["include_nsfw"] is False
    assert r["total_results"] == 40 and r["count"] == 2 and r["next_offset"] == 2
    assert r["matches"][0]["channel"] == "android" and r["matches"][0]["trust"] == "maintainer"
    assert r["matches"][1]["channel"] == "ble-thread"  # thread name from the side array


def test_search_validation(client) -> None:
    with pytest.raises(discord.DiscordError, match="at least one"):
        client.search()
    with pytest.raises(discord.DiscordError, match="sort"):
        client.search("x", sort="newest")
    with pytest.raises(discord.DiscordError, match="offset"):
        client.search("x", offset=99999)


def test_search_limit_is_capped_and_next_offset_ends(client) -> None:
    r = client.search("x", limit=500, offset=9970)
    assert client._t.last_search["limit"] == discord.SEARCH_MAX
    assert (
        r["next_offset"] is None
    )  # 9972 > SEARCH_OFFSET_MAX? no — 9972 ≤ 9975 but hits < total...
    r = client.search("x", limit=25, offset=38)
    assert r["next_offset"] is None  # 38 + 2 = 40 == total


def test_status_never_leaks_token(monkeypatch) -> None:
    monkeypatch.setenv(discord.TOKEN_ENV, "SECRET123")
    rep = discord.status(discord.Client(transport=FakeTransport()))
    assert rep["ok"] and rep["guilds"] == [{"id": GUILD, "name": "Meshtastic"}]
    assert "SECRET123" not in repr(rep)


# -- server surface ----------------------------------------------------------

TOOLS = {
    "discord_status",
    "discord_channels",
    "discord_search",
    "discord_mentions",
    "discord_read",
    "discord_context",
    "discord_thread",
    "discord_forum_posts",
    "discord_pins",
    "discord_member",
}


def test_server_registers_discord_tools_when_token_present() -> None:
    from meshtastic_mcp import server

    tools = {
        tool.name: tool
        for key, tool in server.app.local_provider._components.items()
        if key.startswith("tool:")
    }
    if not server.CAPS.discord:
        assert not (TOOLS & set(tools))
        pytest.skip("discord capability inactive — tools not registered")
    for n in TOOLS:
        ann = tools[n].annotations
        assert (
            ann is not None and ann.readOnlyHint and ann.openWorldHint and not ann.destructiveHint
        )
    assert "trust" in (tools["discord_search"].description or "")
