import datetime as dt
import os
import shutil
import time
from pathlib import Path

import pytest

from monsterbot import importer
from monsterbot.db import Database

SAMPLES = Path(__file__).parent / "samples"
GIFT = SAMPLES / "2024-08-30 00.00 GIFT_STATS -R-.xlsx"
GUILD = SAMPLES / "2024-08-30 00.00 GUILD_LIST -R-.xlsx"
OLD_GIFT = SAMPLES / "2024-28-8 00-00 -R-.xlsx"
OLD_GUILD = SAMPLES / "Guild-2024-28-8 00-00 -R-.xlsx"


def test_gift_stats_detected_by_header_and_summary_rows_skipped():
    parsed = importer.parse_file(GIFT)
    assert parsed.kind == "hunts"
    assert parsed.day == dt.date(2024, 8, 30)
    assert len(parsed.rows) == 95  # 97 data rows minus "Total" and "Anonymous"
    first = parsed.rows[0]
    assert first["user_id"] == 100000001 and first["name"] == "Player001"
    assert first["hunt"] == 4 and first["l2_hunt"] == 4 and first["points_hunt"] == 4
    assert first["hunt_goal_pct"] == 0.08 and first["purchase_goal_pct"] == 0  # both kept, no longer overwritten
    assert first["first_hunt"] == "2024-08-29 01:01:46"


def test_guild_list_detected_and_short_rows_padded():
    parsed = importer.parse_file(OLD_GUILD)
    assert parsed.kind == "guild_list"
    aerith = next(r for r in parsed.rows if r["name"] == "Player099")  # this row has 9 cells, not 10
    assert aerith["old_name"] is None and aerith["kills"] == 183647
    last = importer.parse_file(GUILD).rows[-1]
    assert last == {
        "user_id": 100000042, "name": "Player042", "rank": "RANK3", "might": 1606637325, "old_might": 1608182613,
        "might_diff": -1545288, "kills": 305010481, "old_kills": 304383176, "kills_diff": 627305, "old_name": None,
    }


def test_day_month_order_resolved(tmp_path):
    ydm = tmp_path / "2024-28-8 00-00 -R-.xlsx"
    ydm.touch()
    assert importer.file_day(ydm) == dt.date(2024, 8, 28)

    # Ambiguous 2024-03-04: pick the reading closest to the file's modified time.
    ambiguous = tmp_path / "2024-03-04 00.00 GIFT_STATS x.xlsx"
    ambiguous.touch()
    april_3 = dt.datetime(2024, 4, 3, 12).timestamp()
    os.utime(ambiguous, (april_3, april_3))
    assert importer.file_day(ambiguous) == dt.date(2024, 4, 3)

    no_date = tmp_path / "export.xlsx"
    no_date.touch()
    assert importer.file_day(no_date) == dt.date.today()


def test_unknown_file_rejected(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Something", "Else"])
    path = tmp_path / "other.xlsx"
    wb.save(path)
    with pytest.raises(importer.UnknownFileError):
        importer.parse_file(path)


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "LordsBot" / "100000001" / "stats" / "exported"
    folder.mkdir(parents=True)
    db = Database(tmp_path / "test.db")
    link_id = db.add_link(str(folder), "-R-", 111, 222, post_report=True)
    link = next(l for l in db.links() if l["id"] == link_id)
    return db, folder, link


def _age(path, seconds=120):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_import_is_recorded_once_and_idempotent(setup):
    db, folder, link = setup
    gift = shutil.copy(GIFT, folder)
    _age(gift)
    fresh = shutil.copy(GUILD, folder)  # still being written -> skipped for now
    assert importer.pending_files(db, link) == [Path(gift)]

    parsed = importer.import_file(db, link, Path(gift))
    assert parsed and len(db.day_rows("hunts", 111, parsed.day)) == 95
    assert importer.pending_files(db, link) == []

    db.forget_import(gift, link["id"])  # "Re-import" button
    importer.import_file(db, link, Path(gift))
    assert len(db.day_rows("hunts", 111, parsed.day)) == 95  # upsert, no duplicates
    _age(fresh)
    assert importer.pending_files(db, link) == [Path(fresh)]


def test_failed_import_recorded_not_raised(setup):
    db, folder, link = setup
    broken = folder / "2024-08-30 broken.xlsx"
    broken.write_bytes(b"not a workbook")
    assert importer.import_file(db, link, broken) is None
    [row] = db.recent_imports()
    assert row["error"] and row["rows"] == 0
    assert importer.pending_files(db, link) == []  # not retried every minute


def test_discover_folders(tmp_path):
    (tmp_path / "a" / "123" / "stats" / "exported").mkdir(parents=True)
    (tmp_path / "b" / "exported").mkdir(parents=True)  # not under "stats"
    assert importer.discover_folders(tmp_path) == [tmp_path / "a" / "123" / "stats" / "exported"]
    assert importer.discover_folders(tmp_path / "missing") == []


def test_should_report_only_fresh_files_with_channel():
    parsed = importer.ParsedFile("hunts", dt.date(2024, 8, 30), [])
    link = {"post_report": 1, "channel_id": 5}
    assert importer.should_report(link, parsed, today=dt.date(2024, 8, 31))
    assert not importer.should_report(link, parsed, today=dt.date(2024, 9, 5))  # backfill
    assert not importer.should_report({**link, "post_report": 0}, parsed, today=dt.date(2024, 8, 31))
    assert not importer.should_report({**link, "channel_id": None}, parsed, today=dt.date(2024, 8, 31))


def test_import_folder_counts_and_recursion(tmp_path):
    db = Database(tmp_path / "t.db")
    archive = tmp_path / "archive"
    (archive / "2024").mkdir(parents=True)
    shutil.copy(GIFT, archive)
    shutil.copy(GUILD, archive / "2024")
    (archive / "broken.xlsx").write_bytes(b"nope")
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Other"])
    wb.save(archive / "other.xlsx")

    assert importer.import_folder(db, archive, 111) == {"imported": 1, "skipped": 1, "failed": 1}
    assert importer.import_folder(db, archive, 111, recursive=True) == {"imported": 2, "skipped": 1, "failed": 1}
    assert len(db.day_rows("guild_list", 111, dt.date(2024, 8, 30))) == 95
    manual = [r for r in db.recent_imports() if r["rows"]]
    assert {r["link_id"] for r in manual} == {0} and {r["discord_guild_id"] for r in manual} == {111}
