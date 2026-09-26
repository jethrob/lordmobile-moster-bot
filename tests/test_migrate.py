import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from migrate_from_mongo import GUILD_LIST_FIELDS, HUNT_FIELDS, convert, migrate  # noqa: E402

from monsterbot.db import Database  # noqa: E402

DAY = dt.datetime(2024, 8, 30)


class FakeMongo(dict):
    def __getitem__(self, name):
        return type("Coll", (), {"find": lambda _self: iter(self.get(name, []))})()


def test_convert_skips_summary_and_invalid_docs():
    assert convert({"GuildID": "1", "UserID": 0, "Name": "Total", "DateCreated": DAY}, HUNT_FIELDS) is None
    assert convert({"GuildID": "x", "UserID": 5, "Name": "A", "DateCreated": DAY}, HUNT_FIELDS) is None
    guild, day, row = convert({"GuildID": "1", "UserID": 5, "Name": "A", "DateCreated": DAY, "GoalPercentage": 0.5,
                               "FirstHuntTime": DAY, "Hunt": 3}, HUNT_FIELDS)
    assert (guild, day) == (1, dt.date(2024, 8, 30))
    assert row["hunt"] == 3 and row["purchase_goal_pct"] == 0.5 and row["hunt_goal_pct"] is None
    assert row["first_hunt"] == "2024-08-30 00:00:00"


def test_migrate(tmp_path):
    mongo = FakeMongo(
        playerhuntings=[{"GuildID": "1", "UserID": 5, "Name": "A", "DateCreated": DAY, "Hunt": 3, "PointsHunt": 9}],
        playerguildlist=[{"GuildID": "1", "UserID": 5, "Name": "A", "DateCreated": DAY, "Kills": 100,
                          "KillsDiffrence": 7}],
        users=[{"UserId": 42, "GuildID": "1", "IGG": 5}, {"UserId": "bad"}],
        playerbanks=[{"GuildID": "1"}],
    )
    db = Database(tmp_path / "t.db")
    assert migrate(mongo, db) == {"hunts": 1, "guild_list": 1, "users": 1}
    assert migrate(mongo, db) == {"hunts": 1, "guild_list": 1, "users": 1}  # rerunnable
    assert db.day_rows("hunts", 1, DAY.date())[0]["points_hunt"] == 9
    assert db.day_rows("guild_list", 1, DAY.date())[0]["kills_diff"] == 7
    assert db.igg_for_user(42, 1) == 5
    assert set(GUILD_LIST_FIELDS) == set(db.day_rows("guild_list", 1, DAY.date())[0]) - {"day", "discord_guild_id"}
