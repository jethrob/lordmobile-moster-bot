"""SQLite storage: settings, folder->Discord links, imported stats, and the queries the bot needs."""
import datetime as dt
import sqlite3
import threading
from pathlib import Path

HUNT_COLUMNS = [
    "user_id", "name", "total", "hunt", "purchase",
    "l1_hunt", "l2_hunt", "l3_hunt", "l4_hunt", "l5_hunt",
    "l1_purchase", "l2_purchase", "l3_purchase", "l4_purchase", "l5_purchase",
    "points_hunt", "hunt_goal_pct", "points_purchase", "purchase_goal_pct",
    "first_hunt", "last_hunt",
]
GUILD_LIST_COLUMNS = [
    "user_id", "name", "rank", "might", "old_might", "might_diff",
    "kills", "old_kills", "kills_diff", "old_name",
]
TABLES = {"hunts": HUNT_COLUMNS, "guild_list": GUILD_LIST_COLUMNS}

# Append-only: each entry upgrades the schema by one version (PRAGMA user_version).
MIGRATIONS = [
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE links (
        id INTEGER PRIMARY KEY,
        folder TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        discord_guild_id INTEGER NOT NULL,
        channel_id INTEGER,
        post_report INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE users (
        discord_user_id INTEGER, discord_guild_id INTEGER, igg_id INTEGER,
        PRIMARY KEY (discord_user_id, discord_guild_id)
    );
    CREATE TABLE hunts (
        day TEXT, discord_guild_id INTEGER, user_id INTEGER, name TEXT,
        total INTEGER, hunt INTEGER, purchase INTEGER,
        l1_hunt INTEGER, l2_hunt INTEGER, l3_hunt INTEGER, l4_hunt INTEGER, l5_hunt INTEGER,
        l1_purchase INTEGER, l2_purchase INTEGER, l3_purchase INTEGER, l4_purchase INTEGER, l5_purchase INTEGER,
        points_hunt INTEGER, hunt_goal_pct REAL, points_purchase INTEGER, purchase_goal_pct REAL,
        first_hunt TEXT, last_hunt TEXT,
        PRIMARY KEY (day, discord_guild_id, user_id)
    );
    CREATE TABLE guild_list (
        day TEXT, discord_guild_id INTEGER, user_id INTEGER, name TEXT, rank TEXT,
        might INTEGER, old_might INTEGER, might_diff INTEGER,
        kills INTEGER, old_kills INTEGER, kills_diff INTEGER, old_name TEXT,
        PRIMARY KEY (day, discord_guild_id, user_id)
    );
    CREATE TABLE imported_files (
        path TEXT, link_id INTEGER, imported_at TEXT, kind TEXT, day TEXT, rows INTEGER, error TEXT,
        PRIMARY KEY (path, link_id)
    );
    """,
    # 2: remember the Discord server per imported file (manual folder imports have no link, link_id = 0)
    """
    ALTER TABLE imported_files ADD COLUMN discord_guild_id INTEGER;
    UPDATE imported_files SET discord_guild_id = (SELECT discord_guild_id FROM links WHERE links.id = link_id);
    """,
]
MANUAL_IMPORT = 0  # link_id used for one-off folder imports from the Data page
DEFAULT_INACTIVE_DAYS = 3


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()  # one connection per thread: the importer runs in worker threads
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    def _all(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def _one(self, sql, *args):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def backup(self, target: Path):
        dest = sqlite3.connect(target)
        with dest:
            self.conn.backup(dest)
        dest.close()

    # --- settings -------------------------------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self._one("SELECT value FROM settings WHERE key = ?", key)
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- links ----------------------------------------------------------------------------------
    def links(self, enabled_only=False):
        sql = "SELECT * FROM links" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY folder, id"
        return self._all(sql)

    def add_link(self, folder, label, discord_guild_id, channel_id, post_report):
        cur = self.conn.execute(
            "INSERT INTO links (folder, label, discord_guild_id, channel_id, post_report) VALUES (?, ?, ?, ?, ?)",
            (folder, label, discord_guild_id, channel_id, int(post_report)),
        )
        return cur.lastrowid

    def update_link(self, link_id, **fields):
        allowed = {"label", "channel_id", "post_report", "enabled"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"can only update {sorted(allowed)}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE links SET {assignments} WHERE id = ?", (*fields.values(), link_id))

    def delete_link(self, link_id):
        self.conn.execute("DELETE FROM links WHERE id = ?", (link_id,))

    # --- imports --------------------------------------------------------------------------------
    def is_imported(self, path, link_id):
        return self._one("SELECT 1 AS x FROM imported_files WHERE path = ? AND link_id = ?", str(path), link_id) is not None

    def record_import(self, path, link_id, guild_id, kind=None, day=None, rows=0, error=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO imported_files"
            " (path, link_id, discord_guild_id, imported_at, kind, day, rows, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(path), link_id, guild_id, dt.datetime.now().isoformat(timespec="seconds"), kind, day, rows, error),
        )

    def imported_file(self, path, link_id):
        return self._one("SELECT * FROM imported_files WHERE path = ? AND link_id = ?", str(path), link_id)

    def forget_import(self, path, link_id):
        self.conn.execute("DELETE FROM imported_files WHERE path = ? AND link_id = ?", (str(path), link_id))

    def recent_imports(self, limit=50):
        return self._all(
            "SELECT f.*, l.label, l.folder FROM imported_files f LEFT JOIN links l ON l.id = f.link_id"
            " ORDER BY f.imported_at DESC LIMIT ?",
            limit,
        )

    # --- clean-up (Data page) ---------------------------------------------------------------------
    def data_summary(self):
        """Rows, players and date range per Discord server and table."""
        return self._all(
            "SELECT 'hunts' AS kind, discord_guild_id, COUNT(*) AS rows, COUNT(DISTINCT user_id) AS players,"
            " MIN(day) AS from_day, MAX(day) AS to_day FROM hunts GROUP BY discord_guild_id"
            " UNION ALL SELECT 'guild_list', discord_guild_id, COUNT(*), COUNT(DISTINCT user_id), MIN(day), MAX(day)"
            " FROM guild_list GROUP BY discord_guild_id ORDER BY discord_guild_id, kind"
        )

    def delete_older_than(self, days: int, guild=None) -> int:
        """Delete stats older than `days` days. Import history is kept, so those files aren't imported again."""
        return self._delete("day < ?", [_since(days)], guild)

    def delete_all_data(self, guild=None, forget_files=True) -> int:
        """Delete all stats (every server, or one). With forget_files, linked folders import their files again."""
        deleted = self._delete("1 = 1", [], guild)
        if forget_files:
            where, args = ("discord_guild_id = ?", [guild]) if guild is not None else ("1 = 1", [])
            self.conn.execute(f"DELETE FROM imported_files WHERE {where}", args)
        return deleted

    def _delete(self, where, args, guild) -> int:
        if guild is not None:
            where, args = f"{where} AND discord_guild_id = ?", [*args, guild]
        with self.conn:
            self.conn.execute("BEGIN")
            deleted = sum(self.conn.execute(f"DELETE FROM {t} WHERE {where}", args).rowcount for t in TABLES)
        self.conn.execute("VACUUM")  # give the disk space back
        return deleted

    def upsert_rows(self, kind, discord_guild_id, day: dt.date, rows: list[dict]):
        """Insert or replace one day's rows. Idempotent, so re-importing a file is harmless."""
        columns = TABLES[kind]
        all_cols = ["day", "discord_guild_id", *columns]
        sql = (
            f"INSERT OR REPLACE INTO {kind} ({', '.join(all_cols)}) VALUES ({', '.join('?' * len(all_cols))})"
        )
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.executemany(sql, [(day.isoformat(), discord_guild_id, *(r[c] for c in columns)) for r in rows])

    # --- users ----------------------------------------------------------------------------------
    def link_user(self, discord_user_id, discord_guild_id, igg_id):
        self.conn.execute(
            "INSERT OR REPLACE INTO users (discord_user_id, discord_guild_id, igg_id) VALUES (?, ?, ?)",
            (discord_user_id, discord_guild_id, igg_id),
        )

    def igg_for_user(self, discord_user_id, discord_guild_id):
        row = self._one(
            "SELECT igg_id FROM users WHERE discord_user_id = ? AND discord_guild_id = ?", discord_user_id, discord_guild_id
        )
        return row["igg_id"] if row else None

    # --- queries used by slash commands and reports ---------------------------------------------
    def find_player(self, guild, *, user_id=None, name=None):
        """Latest known (user_id, name, day, left) for an IGG id or exact castle name (case-insensitive).

        Players who left are still found, so their history can be looked up; `left` says so.
        """
        where, arg = ("user_id = ?", user_id) if user_id is not None else ("name = ? COLLATE NOCASE", name)
        found = self._one(
            f"SELECT user_id, name, day FROM (SELECT user_id, name, day FROM hunts WHERE discord_guild_id = ? AND {where}"
            f" UNION ALL SELECT user_id, name, day FROM guild_list WHERE discord_guild_id = ? AND {where})"
            " ORDER BY day DESC LIMIT 1",
            guild, arg, guild, arg,
        )
        if found:
            cutoffs = [c for c in (self.active_cutoff("hunts", guild), self.active_cutoff("guild_list", guild)) if c]
            found["left"] = bool(cutoffs) and found["day"] < min(cutoffs)
        return found

    # --- players who left the guild ---------------------------------------------------------------
    def inactive_days(self) -> int:
        return int(self.get_setting("inactive_days", DEFAULT_INACTIVE_DAYS))

    def active_cutoff(self, table, guild) -> str:
        """Players last seen before this day count as having left the guild ('' = nobody hidden).

        Measured from the newest export, not from today, so a pause in imports doesn't hide everyone.
        """
        days = self.inactive_days()
        latest = self._one(f"SELECT MAX(day) AS d FROM {table} WHERE discord_guild_id = ?", guild)["d"]
        if not days or not latest:
            return ""
        return (dt.date.fromisoformat(latest) - dt.timedelta(days=days)).isoformat()

    def search_players(self, guild, name_part):
        # SQLite returns the bare `name` from the row holding MAX(day), i.e. the player's current name.
        return self._all(
            "SELECT user_id, name FROM (SELECT user_id, name, MAX(day) AS last_seen FROM hunts"
            " WHERE discord_guild_id = ? GROUP BY user_id) WHERE last_seen >= ? AND name LIKE ? ESCAPE '\\'"
            " ORDER BY name COLLATE NOCASE",
            guild, self.active_cutoff("hunts", guild),
            "%" + name_part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
        )

    def player_hunt_totals(self, guild, user_id, days):
        return self._one(
            "SELECT COUNT(*) AS days, MIN(day) AS from_day, MAX(day) AS to_day,"
            " SUM(hunt) AS hunt, SUM(points_hunt) AS points_hunt,"
            " SUM(l1_hunt) AS l1_hunt, SUM(l2_hunt) AS l2_hunt, SUM(l3_hunt) AS l3_hunt,"
            " SUM(l4_hunt) AS l4_hunt, SUM(l5_hunt) AS l5_hunt,"
            " SUM(purchase) AS purchase, SUM(points_purchase) AS points_purchase,"
            " SUM(l1_purchase) AS l1_purchase, SUM(l2_purchase) AS l2_purchase, SUM(l3_purchase) AS l3_purchase,"
            " SUM(l4_purchase) AS l4_purchase, SUM(l5_purchase) AS l5_purchase"
            " FROM hunts WHERE discord_guild_id = ? AND user_id = ? AND day >= ?",
            guild, user_id, _since(days),
        )

    def player_kills(self, guild, user_id, days):
        return self._one(
            "SELECT MAX(kills) - MIN(kills) AS kills, MIN(day) AS from_day, MAX(day) AS to_day, COUNT(*) AS days"
            " FROM guild_list WHERE discord_guild_id = ? AND user_id = ? AND day >= ?",
            guild, user_id, _since(days),
        )

    def points_per_day(self, guild, days, kind="hunt"):
        """Average hunt or purchase points per day with data, per player (for /hunts and /purchases)."""
        column = {"hunt": "points_hunt", "purchase": "points_purchase"}[kind]
        return self._all(
            f"SELECT user_id, {_latest_name('hunts')} AS name, COUNT(*) AS days, SUM({column}) AS points,"
            f" CAST(SUM({column}) AS REAL) / COUNT(*) AS average"
            " FROM hunts h WHERE discord_guild_id = ? AND day >= ? GROUP BY user_id HAVING MAX(day) >= ?",
            guild, _since(days), self.active_cutoff("hunts", guild),
        )

    def kills_gained(self, guild, days):
        return self._all(
            f"SELECT user_id, {_latest_name('guild_list')} AS name, MAX(kills) - MIN(kills) AS kills, COUNT(*) AS days"
            " FROM guild_list h WHERE discord_guild_id = ? AND day >= ? GROUP BY user_id HAVING MAX(day) >= ?",
            guild, _since(days), self.active_cutoff("guild_list", guild),
        )

    def day_rows(self, kind, guild, day: dt.date):
        if kind not in TABLES:
            raise ValueError(kind)
        return self._all(f"SELECT * FROM {kind} WHERE discord_guild_id = ? AND day = ?", guild, day.isoformat())


def _latest_name(table):
    return (
        f"(SELECT name FROM {table} x WHERE x.discord_guild_id = h.discord_guild_id AND x.user_id = h.user_id"
        " ORDER BY day DESC LIMIT 1)"
    )


def _since(days) -> str:
    return (dt.date.today() - dt.timedelta(days=abs(int(days)))).isoformat()

