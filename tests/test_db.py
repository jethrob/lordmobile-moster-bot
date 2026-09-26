import datetime as dt
import sqlite3

import pytest

from monsterbot.db import GUILD_LIST_COLUMNS, HUNT_COLUMNS, MIGRATIONS, Database

GUILD = 111
TODAY = dt.date.today()


def hunt(user_id, name, hunt=0, points=0, purchase=0, purchase_points=0):
    row = dict.fromkeys(HUNT_COLUMNS, 0)
    return {**row, "user_id": user_id, "name": name, "hunt": hunt, "points_hunt": points,
            "purchase": purchase, "points_purchase": purchase_points, "l1_hunt": hunt}


def member(user_id, name, kills):
    return {**dict.fromkeys(GUILD_LIST_COLUMNS), "user_id": user_id, "name": name, "kills": kills}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    ago = lambda n: TODAY - dt.timedelta(days=n)
    d.upsert_rows("hunts", GUILD, ago(40), [hunt(1, "Alice", 50, 500)])  # outside a 30-day window
    d.upsert_rows("hunts", GUILD, ago(2), [hunt(1, "Alice", 10, 100, 1, 20), hunt(2, "Bob", 0, 0)])
    d.upsert_rows("hunts", GUILD, ago(1), [hunt(1, "Alice2", 20, 300, 2, 40), hunt(2, "Bob", 4, 40)])
    d.upsert_rows("hunts", 999, ago(1), [hunt(1, "OtherGuild", 99, 999)])  # other Discord server
    d.upsert_rows("guild_list", GUILD, ago(40), [member(1, "Alice", 0)])
    d.upsert_rows("guild_list", GUILD, ago(2), [member(1, "Alice", 1000), member(2, "Bob", 50)])
    d.upsert_rows("guild_list", GUILD, ago(1), [member(1, "Alice2", 1500), member(2, "Bob", 50)])
    return d


def test_migrations_are_rerunnable(tmp_path):
    Database(tmp_path / "x.db").set_setting("a", "1")
    again = Database(tmp_path / "x.db")
    assert again.get_setting("a") == "1"
    assert again.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_player_totals_respect_window_and_guild(db):
    totals = db.player_hunt_totals(GUILD, 1, days=30)
    assert totals["days"] == 2 and totals["hunt"] == 30 and totals["points_hunt"] == 400
    assert totals["purchase"] == 3 and totals["points_purchase"] == 60 and totals["l1_hunt"] == 30
    assert db.player_hunt_totals(GUILD, 1, days=60)["hunt"] == 80


def test_find_player_uses_latest_name(db):
    found = db.find_player(GUILD, user_id=1)
    assert (found["user_id"], found["name"], found["left"]) == (1, "Alice2", False)
    assert db.find_player(GUILD, name="alice2")["user_id"] == 1  # case-insensitive
    assert db.find_player(GUILD, name="OtherGuild") is None


def test_search_players_matches_current_name_and_escapes_wildcards(db):
    assert [p["name"] for p in db.search_players(GUILD, "ali")] == ["Alice2"]
    assert db.search_players(GUILD, "%") == []


def test_points_per_day(db):
    by_user = {r["user_id"]: r for r in db.points_per_day(GUILD, 30)}
    assert by_user[1]["name"] == "Alice2" and by_user[1]["average"] == 200
    assert by_user[2]["average"] == 20
    assert {r["user_id"]: r["average"] for r in db.points_per_day(GUILD, 30, "purchase")} == {1: 30, 2: 0}


def test_kills_gained(db):
    assert {r["name"]: r["kills"] for r in db.kills_gained(GUILD, 30)} == {"Alice2": 500, "Bob": 0}
    assert db.player_kills(GUILD, 1, 30)["kills"] == 500


def test_links_and_users(db):
    link_id = db.add_link("C:/x", "-R-", GUILD, 5, post_report=False)
    db.update_link(link_id, post_report=1, enabled=0)
    assert db.links()[0]["post_report"] == 1 and db.links(enabled_only=True) == []
    with pytest.raises(ValueError):
        db.update_link(link_id, folder="evil")
    db.link_user(42, GUILD, 1)
    assert db.igg_for_user(42, GUILD) == 1 and db.igg_for_user(42, 999) is None


def test_players_missing_from_exports_are_hidden(db):
    db.upsert_rows("hunts", GUILD, TODAY - dt.timedelta(days=10), [hunt(3, "Carol", 50, 5000)])
    db.upsert_rows("guild_list", GUILD, TODAY - dt.timedelta(days=10), [member(3, "Carol", 0)])
    db.upsert_rows("guild_list", GUILD, TODAY - dt.timedelta(days=9), [member(3, "Carol", 9999)])
    assert db.inactive_days() == 3
    assert 3 not in {r["user_id"] for r in db.points_per_day(GUILD, 30)}
    assert "Carol" not in {r["name"] for r in db.kills_gained(GUILD, 30)}
    assert db.search_players(GUILD, "carol") == []
    carol = db.find_player(GUILD, name="Carol")  # direct lookups still work, flagged as left
    assert carol["left"] is True and db.find_player(GUILD, name="Bob")["left"] is False

    db.set_setting("inactive_days", "0")  # 0 = never hide
    assert 3 in {r["user_id"] for r in db.points_per_day(GUILD, 30)}
    assert [p["name"] for p in db.search_players(GUILD, "carol")] == ["Carol"]
    assert db.find_player(GUILD, name="Carol")["left"] is False

    db.set_setting("inactive_days", "20")
    assert "Carol" in {r["name"] for r in db.kills_gained(GUILD, 30)}


def test_hiding_is_relative_to_newest_export_not_today(tmp_path):
    d = Database(tmp_path / "t.db")
    long_ago = TODAY - dt.timedelta(days=100)  # imports stopped months ago: nobody should vanish
    d.upsert_rows("hunts", GUILD, long_ago, [hunt(1, "Alice", 5, 50)])
    assert [r["user_id"] for r in d.points_per_day(GUILD, 365)] == [1]


def test_migration_2_backfills_server_of_existing_imports(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.executescript(f"BEGIN; {MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
    raw.execute("INSERT INTO links (id, folder, discord_guild_id) VALUES (7, 'C:/x', 555)")
    raw.execute("INSERT INTO imported_files (path, link_id) VALUES ('C:/x/a.xlsx', 7)")
    raw.close()
    assert Database(path).imported_file("C:/x/a.xlsx", 7)["discord_guild_id"] == 555


def test_delete_older_than(db):
    assert db.delete_older_than(30, guild=999) == 0  # other server has nothing that old
    assert db.delete_older_than(30) == 2  # Alice's 40-day-old hunt + guild_list rows
    assert db.player_hunt_totals(GUILD, 1, days=60)["hunt"] == 30


def test_delete_all_data_per_server_and_forget_files(db):
    db.record_import("C:/a.xlsx", 1, GUILD, "hunts", "2024-01-01", 2)
    db.record_import("C:/b.xlsx", 2, 999, "hunts", "2024-01-01", 1)
    assert db.delete_all_data(guild=999, forget_files=True) == 1
    assert db.is_imported("C:/a.xlsx", 1) and not db.is_imported("C:/b.xlsx", 2)
    assert db.delete_all_data(forget_files=False) == 10
    assert db.data_summary() == [] and db.is_imported("C:/a.xlsx", 1)


def test_castle_name_from_any_table(db):
    assert db.castle_name("1") == "Alice2" and db.castle_name(2) == "Bob" and db.castle_name("404") is None
