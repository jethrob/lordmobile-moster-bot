import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from monsterbot import bank, bot
from monsterbot.db import Database
from tests.test_commands import FakeInteraction
from tests.test_db import GUILD, hunt

TODAY = __import__("datetime").date.today()


def write_bank(castle_dir, accounts, enabled=True):
    """Same layout as LordsBot's banksettings.json (extra fields included, names made up)."""
    castle_dir.mkdir(parents=True, exist_ok=True)
    (castle_dir / "stats" / "exported").mkdir(parents=True, exist_ok=True)
    data = {"enableBank": enabled, "cmdPrefix": "j", "adminRssLimit": [0] * 5, "guildCommands": [],
            "accountData": [{"highAuth": False, "UserID": uid, "AccountName": name, "deferAccountName": "",
                             "DeferID": 0, "accountBalance": balance, "socialID": 0} for uid, name, balance in accounts]}
    # LordsBot writes a UTF-8 BOM
    (castle_dir / "banksettings.json").write_text(json.dumps(data), encoding="utf-8-sig")
    return castle_dir / "stats" / "exported"


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "t.db")
    main = write_bank(tmp_path / "config" / "111", [(1, "Alice", [10, 20, 30, 40, 50]), (2, "Bob", [0] * 5),
                                                     (3, "Carol", [3_500_000_000, 0, 0, 0, 0])])
    second = write_bank(tmp_path / "config" / "222", [(1, "Alice", [1, 1, 1, 1, 1]), ("bad", "x", [1] * 5),
                                                       (4, "Short", [1, 2])])
    off = write_bank(tmp_path / "config" / "333", [(9, "Hidden", [5] * 5)], enabled=False)
    for folder in (main, main, second, off):  # the same castle linked twice must only count once
        db.add_link(str(folder), "", GUILD, 10, post_report=False)
    return db, tmp_path


def test_read_bank_and_merge_across_castles(setup):
    db, tmp_path = setup
    assert bank.read_bank(tmp_path / "config" / "333") is None  # bank disabled
    assert bank.read_bank(tmp_path / "missing") is None
    accounts, banks = bank.guild_balances(db, GUILD)
    by_name = {a["name"]: a for a in accounts}
    assert banks == 2 and set(by_name) == {"Alice", "Bob", "Carol"}  # malformed entries skipped
    assert (by_name["Alice"]["food"], by_name["Alice"]["gold"], by_name["Alice"]["total"]) == (11, 51, 155)
    assert by_name["Carol"]["total"] == 3_500_000_000
    assert bank.guild_balances(db, 999) == ([], 0)


def test_half_written_file_retries_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(bank.time, "sleep", lambda s: None)
    castle = tmp_path / "c"
    castle.mkdir()
    (castle / "banksettings.json").write_text('{"enableBank": tr', encoding="utf-8")
    with pytest.raises(bank.BankUnavailable):
        bank.read_bank(castle)


def commands(db):
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    bot.register_commands(tree, db)
    return {c.qualified_name: c.callback for c in tree.walk_commands() if isinstance(c, app_commands.Command)}


def call(callback, *args, user_id=42):
    interaction = FakeInteraction(user_id=user_id)
    asyncio.run(callback(interaction, *args))
    return interaction.sent


def test_bank_player_me_and_list(setup):
    db, _ = setup
    cmd = commands(db)
    db.upsert_rows("hunts", GUILD, TODAY, [hunt(1, "Alice", 1, 1)])

    [(_, embed, _)] = call(cmd["bank player"], "alice")
    assert embed.title == "Bank balance for Alice" and embed.fields[0].value == "11\n`11`"
    assert embed.footer.text == "Live from 2 LordsBot banks"
    [(text, _, ephemeral)] = call(cmd["bank player"], "Nobody")
    assert "No bank account" in text and ephemeral

    [(text, _, _)] = call(cmd["bank me"])
    assert "Link your IGG ID" in text
    db.link_user(42, GUILD, 3)
    [(_, embed, _)] = call(cmd["bank me"])
    assert embed.title == "Bank balance for Carol" and embed.fields[0].value.startswith("3.50B")

    [(text, _, _)] = call(cmd["bank list"], "total", 25)
    assert text.index("Carol") < text.index("Alice") and "Bob" not in text  # empty accounts left out
    [(text, _, _)] = call(cmd["bank list"], "gold", 25)
    assert "Alice" in text and "Carol" not in text


def test_bank_commands_without_bank(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    cmd = commands(db)
    [(text, _, ephemeral)] = call(cmd["bank list"], "total", 25)
    assert "No bank found" in text and ephemeral

    def busy(*_):
        raise bank.BankUnavailable("busy")

    monkeypatch.setattr(bank, "guild_balances", busy)
    [(text, _, _)] = call(cmd["bank player"], "x")
    assert "being updated" in text
