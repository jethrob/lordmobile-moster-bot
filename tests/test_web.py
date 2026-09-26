import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from monsterbot import web
from monsterbot.db import Database


class FakeChannel(SimpleNamespace):
    def permissions_for(self, member):
        return SimpleNamespace(send_messages=self.can_send)


class FakeGuild(SimpleNamespace):
    def get_channel(self, channel_id):
        return next((c for c in self.text_channels if c.id == channel_id), None)


class FakeClient:
    application_id = 42

    def __init__(self, guilds):
        self.guilds = guilds

    def is_ready(self):
        return True

    def get_guild(self, guild_id):
        return next((g for g in self.guilds if g.id == guild_id), None)


def fake_client():
    channels = [FakeChannel(id=10, name="reports", can_send=True), FakeChannel(id=11, name="readonly", can_send=False)]
    return FakeClient([FakeGuild(id=1, name="<b>Guild</b>", text_channels=channels, me=None)])


def run(coro_fn):
    """Start the admin app on a random port and pass a test client to `coro_fn(client, rt)`."""
    async def go(tmp_path, with_bot):
        rt = web.Runtime(Database(tmp_path / "t.db"))
        rt.client = fake_client() if with_bot else None
        server = TestServer(web.create_app(rt), host="127.0.0.1")
        async with TestClient(server) as client:
            rt.port = server.port
            await coro_fn(client, rt)
    return go


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "LordsBot" / "config" / "123" / "stats" / "exported"
    f.mkdir(parents=True)
    return f


def test_pages_render_and_escape_names(tmp_path, folder):
    async def check(client, rt):
        rt.db.set_setting("root_folder", str(tmp_path / "LordsBot" / "config"))
        for path in ("/setup", "/links", "/activity"):
            resp = await client.get(path)
            assert resp.status == 200, path
        links = await (await client.get("/links")).text()
        assert "&lt;b&gt;Guild&lt;/b&gt;" in links and "<b>Guild</b>" not in links
        assert "#reports" in links and "#readonly" not in links  # only channels the bot can post in
        assert "1 castle(s) found" in links and '<option value="123">123</option>' in links
        setup = await (await client.get("/setup")).text()
        assert "client_id=42" in setup and "Found 1 castle(s)" in setup
        assert "<h2>Castles</h2>" in setup and '<td>123</td>' in setup and "None yet" in setup
        assert "/links?igg=123" in setup  # not linked yet

    asyncio.run(run(check)(tmp_path, with_bot=True))


def test_foreign_host_and_cross_site_post_blocked(tmp_path):
    async def check(client, rt):
        assert (await client.get("/setup", headers={"Host": "evil.example"})).status == 403
        resp = await client.post("/setup", data={"token": "x"}, headers={"Origin": "http://evil.example"},
                                 allow_redirects=False)
        assert resp.status == 403 and rt.db.get_setting("bot_token") is None
        ok = await client.post("/setup", data={"token": "abc"}, headers={"Origin": f"http://127.0.0.1:{rt.port}"},
                               allow_redirects=False)
        assert ok.status == 303 and rt.db.get_setting("bot_token") == "abc" and rt.restart.is_set()

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_token_is_never_rendered(tmp_path):
    async def check(client, rt):
        rt.db.set_setting("bot_token", "super-secret-token")
        assert "super-secret-token" not in await (await client.get("/setup")).text()

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_setup_rejects_missing_root_folder(tmp_path):
    async def check(client, rt):
        resp = await client.post("/setup", data={"root": str(tmp_path / "nope")}, allow_redirects=False)
        assert "Folder%20not%20found" in resp.headers["Location"] and rt.db.get_setting("root_folder") is None

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_add_link_validates_and_toggles(tmp_path, folder):
    async def check(client, rt):
        rt.db.set_setting("root_folder", str(tmp_path / "LordsBot"))  # parent of config\ also works
        post = lambda path, data: client.post(path, data=data, allow_redirects=False)
        bad = [
            {"igg": "999", "target": "1:10"},  # no folder for this castle
            {"igg": "../123", "target": "1:10"},
            {"igg": "123", "target": "junk"},
            {"igg": "123", "target": "999:10"},  # server the bot isn't in
            {"igg": "123", "target": "1:77"},  # channel not in that server
        ]
        for data in bad:
            await post("/links", data)
        assert rt.db.links() == []

        await post("/links", {"igg": "123", "label": "-R-", "target": "1:10", "post_report": "on"})
        await post("/links", {"igg": " 123 ", "label": "-R-", "target": "1:", "post_report": "on"})
        with_channel, import_only = rt.db.links()
        assert with_channel["folder"] == str(folder)
        assert with_channel["channel_id"] == 10 and with_channel["post_report"] == 1
        assert import_only["channel_id"] is None and import_only["post_report"] == 0  # no channel, no report

        await post(f"/links/{with_channel['id']}", {"action": "enabled"})
        await post(f"/links/{import_only['id']}", {"action": "post_report"})  # refused, no channel
        await post(f"/links/{import_only['id']}", {"action": "delete"})
        [link] = rt.db.links()
        assert link["enabled"] == 0 and link["post_report"] == 1

    asyncio.run(run(check)(tmp_path, with_bot=True))


def test_add_link_needs_connected_bot(tmp_path, folder):
    async def check(client, rt):
        await client.post("/links", data={"igg": "123", "target": "1:10"}, allow_redirects=False)
        assert rt.db.links() == []
        assert "Connect the bot" in await (await client.get("/links")).text()

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_reimport_forgets_file(tmp_path):
    async def check(client, rt):
        rt.db.record_import("C:/x.xlsx", 5, 1, error="boom")
        await client.post("/activity/reimport", data={"path": "C:/x.xlsx", "link_id": "5"}, allow_redirects=False)
        assert not rt.db.is_imported("C:/x.xlsx", 5)
        resp = await client.post("/activity/reimport", data={"path": "x"}, allow_redirects=False)
        assert "Invalid" in resp.headers["Location"]

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_parse_target():
    assert web.parse_target("1:2") == (1, 2) and web.parse_target("1:") == (1, None)
    with pytest.raises(ValueError):
        web.parse_target("abc")


def run_with_backups(coro_fn):
    async def go(tmp_path, with_bot):
        rt = web.Runtime(Database(tmp_path / "t.db"), backup_dir=tmp_path / "backups")
        rt.client = fake_client() if with_bot else None
        server = TestServer(web.create_app(rt), host="127.0.0.1")
        async with TestClient(server) as client:
            rt.port = server.port
            await coro_fn(client, rt)
    return go


def test_setup_saves_inactive_days(tmp_path):
    async def check(client, rt):
        for bad in ("-1", "abc", "400"):
            resp = await client.post("/setup", data={"inactive_days": bad}, allow_redirects=False)
            assert "whole%20number" in resp.headers["Location"]
        await client.post("/setup", data={"inactive_days": "7"}, allow_redirects=False)
        assert rt.db.inactive_days() == 7
        assert 'value="7"' in await (await client.get("/setup")).text()

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_data_page_import_folder(tmp_path):
    import shutil

    from tests.test_importer import GIFT

    archive = tmp_path / "archive"
    archive.mkdir()
    shutil.copy(GIFT, archive)

    async def check(client, rt):
        page_text = await (await client.get("/data")).text()
        assert "No data yet" in page_text and "&lt;b&gt;Guild&lt;/b&gt;" in page_text
        missing = await client.post("/data/import", data={"folder": str(tmp_path / "nope"), "server": "1"},
                                    allow_redirects=False)
        assert "Folder%20not%20found" in missing.headers["Location"]
        resp = await client.post("/data/import", data={"folder": str(archive), "server": "1"}, allow_redirects=False)
        assert resp.headers["Location"].startswith("/activity")
        await rt.import_task
        assert "Hunts" in await (await client.get("/data")).text()
        activity = await (await client.get("/activity")).text()
        assert "Folder import" in activity

        # Manual imports are re-imported immediately (the poller doesn't watch that folder).
        path = str(archive / GIFT.name)
        resp = await client.post("/activity/reimport", data={"path": path, "link_id": "0"}, allow_redirects=False)
        assert "Re-imported" in resp.headers["Location"]

    asyncio.run(run(check)(tmp_path, with_bot=True))


def test_data_cleanup_needs_confirmation_and_backs_up(tmp_path):
    import datetime as dt

    from tests.test_db import hunt

    async def check(client, rt):
        old = dt.date.today() - dt.timedelta(days=400)
        rt.db.upsert_rows("hunts", 1, old, [hunt(1, "Old")])
        rt.db.upsert_rows("hunts", 1, dt.date.today(), [hunt(1, "New")])
        rt.db.record_import("C:/f.xlsx", 3, 1, "hunts", "x", 1)

        resp = await client.post("/data/delete-old", data={"days": "365", "server": "all", "confirm": "yes"},
                                 allow_redirects=False)
        assert "Nothing%20deleted" in resp.headers["Location"]
        assert not (tmp_path / "backups").exists()

        bad_days = await client.post("/data/delete-old", data={"days": "0", "server": "all", "confirm": "DELETE"},
                                     allow_redirects=False)
        assert "whole%20number" in bad_days.headers["Location"]

        resp = await client.post("/data/delete-old", data={"days": "365", "server": "1", "confirm": "DELETE"},
                                 allow_redirects=False)
        assert "Deleted%201%20rows" in resp.headers["Location"]
        assert len(list((tmp_path / "backups").glob("before-cleanup-*.db"))) == 1

        resp = await client.post("/data/delete-all", data={"server": "all", "confirm": "DELETE", "reimport": "on"},
                                 allow_redirects=False)
        assert "Deleted%201%20rows" in resp.headers["Location"]
        assert rt.db.data_summary() == [] and not rt.db.is_imported("C:/f.xlsx", 3)

    asyncio.run(run_with_backups(check)(tmp_path, with_bot=False))


def test_setup_browse_uses_native_picker(tmp_path, folder, monkeypatch):
    chosen = str(tmp_path / "LordsBot" / "config")
    picks = iter([chosen, None])
    monkeypatch.setattr(web.picker, "supported", lambda: True)
    monkeypatch.setattr(web.picker, "pick_folder", lambda start: next(picks))

    async def check(client, rt):
        assert "Browse…" in await (await client.get("/setup")).text()
        resp = await client.post("/setup/browse", allow_redirects=False)
        assert "Found%201%20castle" in resp.headers["Location"] and rt.db.get_setting("root_folder") == chosen
        resp = await client.post("/setup/browse", allow_redirects=False)  # cancelled: setting unchanged
        assert "No%20folder%20chosen" in resp.headers["Location"] and rt.db.get_setting("root_folder") == chosen

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_setup_warns_when_no_castles_found(tmp_path):
    async def check(client, rt):
        rt.db.set_setting("root_folder", str(tmp_path))
        assert "No castle folders found here" in await (await client.get("/setup")).text()

    asyncio.run(run(check)(tmp_path, with_bot=False))


def test_links_show_castle_names(tmp_path, folder):
    import datetime as dt

    from tests.test_db import hunt

    async def check(client, rt):
        rt.db.set_setting("root_folder", str(tmp_path / "LordsBot" / "config"))
        rt.db.upsert_rows("hunts", 1, dt.date.today(), [hunt(123, "<Castle>")])
        rt.db.add_link(str(folder), "-R-", 1, 10, post_report=True)
        text = await (await client.get("/links")).text()
        assert "123 (&lt;Castle&gt;)" in text and "<Castle>" not in text

    asyncio.run(run(check)(tmp_path, with_bot=True))


def test_castles_table_and_link_defaults_to_guild_tag(tmp_path, folder):
    (folder / "2026-07-03 00.00 GIFT_STATS Ax7.xlsx").write_bytes(b"x")
    cache_only = tmp_path / "LordsBot" / "config" / "456" / "stats" / "cache"
    cache_only.mkdir(parents=True)
    (cache_only / "2026-07-03 00-00 CACHE Q&A.json").write_text("x")

    async def check(client, rt):
        rt.db.set_setting("root_folder", str(tmp_path / "LordsBot" / "config"))
        links = await (await client.get("/links?igg=123")).text()
        assert 'value="123"' in links and "[Ax7]" in links and "[Q&amp;A]" in links
        await client.post("/links", data={"igg": "123", "target": "1:10"}, allow_redirects=False)
        assert rt.db.links()[0]["label"] == "Ax7"  # empty guild name -> castle's guild tag
        setup = await (await client.get("/setup")).text()
        assert "<strong>Ax7</strong>" in setup and "#reports" in setup  # linked channel shown
        assert "<strong>Q&amp;A</strong>" in setup and "Turn on stats export" in setup
        assert "/links?igg=456" in setup

    asyncio.run(run(check)(tmp_path, with_bot=True))
