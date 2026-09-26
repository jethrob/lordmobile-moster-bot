"""Slash command handlers, called directly with a fake Discord interaction."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from monsterbot import bot, importer
from monsterbot.db import Database
from tests.test_db import GUILD, hunt, member

TODAY = dt.date.today()


class FakeInteraction:
    def __init__(self, guild_id=GUILD, user_id=42):
        self.guild_id, self.user = guild_id, SimpleNamespace(id=user_id)
        self.sent = []  # (text, embed, ephemeral)
        self._done = False
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer, is_done=lambda: self._done)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, content=None, *, embed=None, ephemeral=False):
        self._done = True
        self.sent.append((content, embed, ephemeral))

    async def _defer(self):
        self._done = True


@pytest.fixture
def commands(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_rows("hunts", GUILD, TODAY - dt.timedelta(days=1), [hunt(1, "Alice", 10, 100), hunt(2, "Bob", 1, 5)])
    db.upsert_rows("guild_list", GUILD, TODAY - dt.timedelta(days=2), [member(1, "Alice", 1000), member(2, "Bob", 0)])
    db.upsert_rows("guild_list", GUILD, TODAY - dt.timedelta(days=1), [member(1, "Alice", 3500), member(2, "Bob", 0)])
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    bot.register_commands(tree, db)
    found = {c.qualified_name: c.callback for c in tree.walk_commands() if isinstance(c, app_commands.Command)}
    return db, found


def call(callback, *args, **kwargs):
    interaction = FakeInteraction()
    asyncio.run(callback(interaction, *args, **kwargs))
    return interaction.sent


def test_player_castlename(commands):
    _, cmd = commands
    [(_, embed, _)] = call(cmd["player castlename"], "alice", 30)
    assert embed.title == "Lords Mobile history for Alice"
    [(text, embed, ephemeral)] = call(cmd["player castlename"], "Nobody", 30)
    assert "Unable to find" in text and ephemeral


def test_player_link_then_discordname(commands):
    db, cmd = commands
    user = SimpleNamespace(id=42, display_name="Me")
    [(text, _, _)] = call(cmd["player discordname"], user, 30)
    assert "hasn't linked" in text
    call(cmd["player link"], 1)
    assert db.igg_for_user(42, GUILD) == 1
    [(_, embed, _)] = call(cmd["player discordname"], user, 30)
    assert "Alice" in embed.title


def test_player_search(commands):
    _, cmd = commands
    [(text, _, _)] = call(cmd["player search"], "b")
    assert "IGG: 2, Name: Bob" in text
    [(text, _, _)] = call(cmd["player search"], "zzz")
    assert "No players found." in text


def test_points_and_kills_queries(commands):
    _, cmd = commands
    [(text, _, _)] = call(cmd["hunts query"], "Above", 50, "descending", 30)
    assert "Alice" in text and "Bob" not in text
    [(text, _, _)] = call(cmd["purchases query"], "Below", 1, "ascending", 30)
    assert "Alice" in text and "Bob" in text
    [(text, _, _)] = call(cmd["kills query"], "Above", 1000, "ascending", 30)
    assert "2.50K" in text and "Bob" not in text
    [(text, _, _)] = call(cmd["kills total"], 30)
    assert "**2.50K**" in text


def test_post_report_skips_when_offline_and_sends_when_ready():
    link = {"id": 1, "channel_id": 10, "discord_guild_id": 1, "label": ""}
    parsed = importer.ParsedFile("hunts", TODAY, [])
    asyncio.run(bot.post_report(None, link, parsed))  # no client: logged, not raised

    sent = []

    async def send(embed):
        sent.append(embed)

    client = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda _id: SimpleNamespace(send=send),
        get_guild=lambda _id: SimpleNamespace(name="My Guild"),
    )
    asyncio.run(bot.post_report(client, link, parsed))
    assert sent[0].title.startswith("📊 My Guild daily report")


def test_importer_loop_reports_fresh_files(tmp_path, monkeypatch):
    import os
    import shutil
    import time

    from tests.test_importer import GIFT

    folder = tmp_path / "exported"
    folder.mkdir()
    copy = shutil.copy(GIFT, folder)
    old = time.time() - 120
    os.utime(copy, (old, old))
    db = Database(tmp_path / "t.db")
    db.add_link(str(folder), "-R-", GUILD, 10, post_report=True)
    monkeypatch.setattr(importer, "should_report", lambda link, parsed: True)
    reported = []

    async def on_imported(link, parsed):
        reported.append(parsed.day)
        raise RuntimeError("Discord down")  # must not stop the loop

    async def one_pass():
        task = asyncio.create_task(importer.run_forever(db, on_imported, interval=3600))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if reported:
                break
        task.cancel()

    asyncio.run(one_pass())
    assert reported == [dt.date(2024, 8, 30)]
    assert db.recent_imports()[0]["rows"] == 95


def test_command_sync_retried_after_failure(tmp_path, monkeypatch):
    calls = []

    async def flaky_sync(self, *, guild=None):
        calls.append(guild)
        if len(calls) == 1:
            raise discord.HTTPException(SimpleNamespace(status=503, reason="down"), "down")
        return []

    monkeypatch.setattr(app_commands.CommandTree, "sync", flaky_sync)
    client = bot.create_client(Database(tmp_path / "t.db"))
    monkeypatch.setattr(type(client), "guilds", property(lambda self: []))
    on_ready = client.on_ready
    with pytest.raises(discord.HTTPException):
        asyncio.run(on_ready())
    asyncio.run(on_ready())  # reconnect: retried
    asyncio.run(on_ready())  # already synced: skipped
    assert calls == [None, None]


def test_player_who_left_is_flagged(commands):
    db, cmd = commands
    db.upsert_rows("hunts", GUILD, TODAY - dt.timedelta(days=20), [hunt(9, "Gone", 3, 30)])
    [(_, embed, _)] = call(cmd["player castlename"], "Gone", 30)
    assert embed.title == "Lords Mobile history for Gone (no longer in the guild exports)"
