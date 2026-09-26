"""One-off: copy the old MonsterBot MongoDB data into monsterbot.db.

    pip install pymongo
    kubectl port-forward svc/mongodb 27017:27017        # in another terminal
    python tools/migrate_from_mongo.py "mongodb://USER:PASS@127.0.0.1:27017/lordsmobiledb" monsterbot.db

Safe to run more than once (rows are upserted). `playerbanks` is intentionally not migrated.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monsterbot.db import Database  # noqa: E402

HUNT_FIELDS = {  # sqlite column -> Mongo field
    "user_id": "UserID", "name": "Name", "total": "Total", "hunt": "Hunt", "purchase": "Purchase",
    **{f"l{i}_hunt": f"L{i}Hunt" for i in range(1, 6)},
    **{f"l{i}_purchase": f"L{i}Purchase" for i in range(1, 6)},
    "points_hunt": "PointsHunt", "points_purchase": "PointsPurchase",
    # The old importer wrote both goal percentages to "GoalPercentage"; the purchase value won.
    "hunt_goal_pct": None, "purchase_goal_pct": "GoalPercentage",
    "first_hunt": "FirstHuntTime", "last_hunt": "LastHuntTime",
}
GUILD_LIST_FIELDS = {
    "user_id": "UserID", "name": "Name", "rank": "Rank", "might": "Might", "old_might": "OldMight",
    "might_diff": "MightDiffrence", "kills": "Kills", "old_kills": "OldKills", "kills_diff": "KillsDiffrence",
    "old_name": "OldName",
}


def convert(doc: dict, fields: dict) -> tuple[int, object, dict] | None:
    """Mongo document -> (discord_guild_id, day, row), or None for summary/invalid rows."""
    try:
        guild, user_id = int(doc["GuildID"]), int(doc["UserID"])
    except (KeyError, TypeError, ValueError):
        return None
    if user_id == 0 or doc.get("Name") is None or doc.get("DateCreated") is None:
        return None
    row = {col: _value(doc.get(src)) if src else None for col, src in fields.items()}
    return guild, doc["DateCreated"].date(), row


def _value(v):
    return v.isoformat(sep=" ") if hasattr(v, "isoformat") else (None if v == "" else v)


def migrate(mongo_db, db: Database) -> dict:
    counts = {}
    for collection, kind, fields in (("playerhuntings", "hunts", HUNT_FIELDS),
                                     ("playerguildlist", "guild_list", GUILD_LIST_FIELDS)):
        groups = defaultdict(list)
        for doc in mongo_db[collection].find():
            converted = convert(doc, fields)
            if converted:
                guild, day, row = converted
                groups[(guild, day)].append(row)
        for (guild, day), rows in groups.items():
            db.upsert_rows(kind, guild, day, rows)
        counts[kind] = sum(len(r) for r in groups.values())
    users = 0
    for doc in mongo_db["users"].find():
        try:
            db.link_user(int(doc["UserId"]), int(doc["GuildID"]), int(doc["IGG"]))
            users += 1
        except (KeyError, TypeError, ValueError):
            continue
    counts["users"] = users
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    import pymongo

    client = pymongo.MongoClient(sys.argv[1])
    print(migrate(client.get_default_database("lordsmobiledb"), Database(sys.argv[2])))
