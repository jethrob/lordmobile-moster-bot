import datetime as dt

from monsterbot import bot
from monsterbot.importer import ParsedFile, parse_file
from tests.test_importer import GIFT, GUILD


def test_compact():
    assert bot.compact(0) == "0" and bot.compact(None) == "0" and bot.compact(999) == "999"
    assert bot.compact(1500) == "1.50K" and bot.compact(2_000_000) == "2.00M" and bot.compact(-3_000) == "-3.00K"
    assert bot.compact(5 * 10**12) == "5000.00B"


def test_chunk_lines_respects_limit():
    lines = [f"line {i:03}" for i in range(300)]
    chunks = bot.chunk_lines(lines, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).splitlines() == lines
    assert bot.chunk_lines([]) == []


def test_filter_players():
    players = [{"n": "a", "v": 5}, {"n": "b", "v": 10}, {"n": "c", "v": 1}]
    assert [p["n"] for p in bot.filter_players(players, "v", "Above", 5, "descending")] == ["b", "a"]
    assert [p["n"] for p in bot.filter_players(players, "v", "Below", 5, "ascending")] == ["c"]


def test_hunt_report_from_real_export():
    embed = bot.report_embed("-R-", parse_file(GIFT))
    assert embed.title == "📊 -R- daily report: 2024-08-30"
    top, zero = embed.fields
    assert top.value.startswith("1. ") and len(top.value.splitlines()) == 5
    assert zero.name == "⚠️ Zero hunts (26)"  # member rows with 0 hunts; Total/Anonymous rows excluded
    assert embed.footer.text == "95 players"


def test_kills_report_and_empty_cases():
    embed = bot.report_embed("-R-", parse_file(GUILD))
    assert embed.fields[0].name == "⚔️ Most kills gained" and embed.fields[0].value.startswith("1. ")
    empty = bot.report_embed("X", ParsedFile("hunts", dt.date(2024, 1, 1), []))
    assert [f.value for f in empty.fields] == ["Nobody hunted", "None 🎉"]


def test_zero_list_truncated_to_discord_field_limit():
    rows = [{"name": f"Player{i:04}", "hunt": 0, "points_hunt": 0} for i in range(500)]
    embed = bot.report_embed("X", ParsedFile("hunts", dt.date(2024, 1, 1), rows))
    assert len(embed.fields[1].value) <= 1024 and embed.fields[1].value.endswith("…")


def test_player_embed():
    hunts = {"days": 2, "from_day": "2024-01-01", "to_day": "2024-01-02", "hunt": 10, "points_hunt": 100,
             "purchase": 0, "points_purchase": 0, **{f"l{i}_{k}": i for i in range(1, 6) for k in ("hunt", "purchase")}}
    embed = bot.player_embed("Alice", 30, hunts, {"days": 2, "kills": 1234, "from_day": "a", "to_day": "b"})
    values = {f.name: f.value for f in embed.fields}
    assert "Avg points/day: 50.00" in values["Hunting"] and values["Purchases"] == "Zero purchases"
    assert "1,234" in values["Kills"]
    none = bot.player_embed("Bob", 30, {"days": 0}, {"days": 0})
    assert [f.value for f in none.fields] == ["No data", "Need guild list data"]


def test_all_commands_register():
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    bot.register_commands(tree, db=None)
    names = sorted(c.qualified_name for c in tree.walk_commands() if isinstance(c, app_commands.Command))
    assert names == ["guild changes", "guild goals", "hunts query", "kills query", "kills total", "might growth",
                     "might top", "player castlename", "player discordname", "player link", "player search",
                     "purchases query"]


def test_command_payload_within_discord_limits():
    """Discord rejects the whole sync if any option breaks its limits (e.g. max_value > 2**53 - 1)."""
    import discord
    from discord import app_commands

    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    bot.register_commands(tree, db=None)

    def options(payload):
        for option in payload.get("options", []):
            yield option
            yield from options(option)

    for command in tree.get_commands():
        payload = command.to_dict(tree)
        assert 1 <= len(payload["description"]) <= 100
        for option in options(payload):
            assert 1 <= len(option.get("description", "x")) <= 100, option["name"]
            for key in ("min_value", "max_value"):
                if key in option:
                    assert -(2**53 - 1) <= option[key] <= 2**53 - 1, (option["name"], key)
            assert len(option.get("choices", [])) <= 25
