# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only Discord bridge: token resolution, channel resolution, paging, search bounds."""

from __future__ import annotations

from typing import Any

import pytest

from meshtastic_mcp import discord

GUILD = "867578229534359593"


def _msg(i: int, content: str, author: str = "alice") -> dict[str, Any]:
    return {
        "id": str(1000 - i),
        "timestamp": f"2026-08-22T08:{59 - i:02d}:00+00:00",
        "author": {"id": "7", "username": author, "global_name": None},
        "content": content,
        "attachments": [{"filename": "log.txt"}] if i == 0 else [],
        "reactions": [{"count": 2}] if i == 0 else [],
    }


class FakeTransport:
    """In-memory Discord: one guild, two text channels, one category, one thread."""

    def __init__(self, pages: dict[str, list[dict[str, Any]]] | None = None):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.pages = pages or {
            "10": [
                _msg(i, f"android msg {i}" + (" BLE stale" if i % 3 == 0 else "")) for i in range(7)
            ],
            "11": [_msg(i, f"firmware msg {i}") for i in range(3)],
            "12": [_msg(i, f"thread msg {i}") for i in range(2)],
        }

    def __call__(self, method: str, path: str, query: dict[str, Any] | None) -> Any:
        self.calls.append((method, path, query))
        assert method == "GET", "read-only bridge must never write"
        if path == "/users/@me/guilds":
            return [{"id": GUILD, "name": "Meshtastic"}]
        if path == f"/guilds/{GUILD}/channels":
            return [
                {"id": "1", "type": 4, "name": "Apps"},
                {"id": "10", "type": 0, "name": "android", "parent_id": "1", "topic": "t"},
                {"id": "11", "type": 0, "name": "firmware", "parent_id": None},
                {"id": "13", "type": 2, "name": "voice"},
                {"id": "14", "type": 15, "name": "help-forum"},
            ]
        if path == f"/guilds/{GUILD}/threads/active":
            return {"threads": [{"id": "12", "name": "ble-thread", "parent_id": "10"}]}
        if path == "/channels/12":
            return {"name": "ble-thread", "parent_id": "10", "thread_metadata": {"archived": False}}
        if path.startswith("/channels/") and path.endswith("/messages"):
            cid = path.split("/")[2]
            msgs = self.pages[cid]
            q = query or {}
            if q.get("before"):
                ids = [m["id"] for m in msgs]
                msgs = msgs[ids.index(q["before"]) + 1 :]
            return msgs[: int(q.get("limit", 50))]
        raise AssertionError(f"unexpected {path}")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> discord.Client:
    monkeypatch.delenv(discord.GUILD_ENV, raising=False)
    return discord.Client(transport=FakeTransport())


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


def test_channels_flatten_category_and_threads_drop_voice(client) -> None:
    chans = client.channels()
    by = {c["name"]: c for c in chans}
    assert set(by) == {"android", "firmware", "help-forum", "ble-thread"}
    assert by["android"]["category"] == "Apps"
    assert by["ble-thread"] == {
        "id": "12",
        "name": "ble-thread",
        "kind": "thread",
        "category": "android",
        "topic": "",
    }
    assert by["help-forum"]["kind"] == "forum"


def test_resolve_channel_accepts_hash_name_and_id(client) -> None:
    assert client.resolve_channel("#Android") == "10"
    assert client.resolve_channel("10") == "10"
    with pytest.raises(discord.DiscordError, match="no channel named"):
        client.resolve_channel("nope")


def test_guild_id_from_env_skips_probe(monkeypatch) -> None:
    monkeypatch.setenv(discord.GUILD_ENV, "42")
    t = FakeTransport()
    assert discord.Client(transport=t).guild_id() == "42"
    assert t.calls == []


def test_messages_are_slimmed_and_capped(client) -> None:
    msgs = client.messages("android", limit=500)
    assert len(msgs) == 7  # fake has 7; request was capped to 100
    first = msgs[0]
    assert first == {
        "id": "1000",
        "timestamp": "2026-08-22T08:59:00+00:00",
        "author": "alice",
        "author_id": "7",
        "bot": False,
        "content": "android msg 0 BLE stale",
        "reply_to": None,
        "attachments": ["log.txt"],
        "reactions": 2,
        "thread_id": None,
    }


def test_search_is_case_insensitive_and_bounded(client) -> None:
    res = client.search("ble STALE", channel="android", limit=2, max_scan=100)
    assert [m["content"] for m in res["matches"]] == [
        "android msg 0 BLE stale",
        "android msg 3 BLE stale",
    ]
    assert res["truncated"] is True
    assert res["channels_scanned"] == 1
    assert res["matches"][0]["channel"] == "android"


def test_search_all_channels_skips_forums_and_reports_scan(client) -> None:
    res = client.search("msg", limit=100, max_scan=100)
    assert res["channels_scanned"] == 3  # android, firmware, ble-thread — not help-forum
    assert res["scanned"] == 12
    assert res["truncated"] is False
    assert res["oldest_seen"] == "2026-08-22T08:53:00+00:00"


def test_search_regex(client) -> None:
    res = client.search(r"msg [45]$", channel="android", regex=True)
    assert sorted(m["content"] for m in res["matches"]) == ["android msg 4", "android msg 5"]


def test_thread_is_oldest_first_with_meta(client) -> None:
    t = client.thread("ble-thread")
    assert t["name"] == "ble-thread" and t["parent_id"] == "10" and t["archived"] is False
    assert [m["content"] for m in t["messages"]] == ["thread msg 1", "thread msg 0"]


def test_status_never_leaks_token(monkeypatch) -> None:
    monkeypatch.setenv(discord.TOKEN_ENV, "SECRET123")
    rep = discord.status(discord.Client(transport=FakeTransport()))
    assert rep["ok"] and rep["guilds"] == [{"id": GUILD, "name": "Meshtastic"}]
    assert "SECRET123" not in repr(rep)


def test_server_registers_discord_tools_when_token_present(monkeypatch) -> None:
    from meshtastic_mcp import server

    tools = {
        tool.name: tool
        for key, tool in server.app.local_provider._components.items()
        if key.startswith("tool:")
    }
    names = {
        "discord_status",
        "discord_channels",
        "discord_read",
        "discord_search",
        "discord_thread",
    }
    if not server.CAPS.discord:
        assert not (names & set(tools))
        pytest.skip("discord capability inactive — tools not registered")
    for n in names:
        ann = tools[n].annotations
        assert (
            ann is not None and ann.readOnlyHint and ann.openWorldHint and not ann.destructiveHint
        )
