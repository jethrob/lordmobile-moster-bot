import asyncio
import datetime as dt

import discord

from monsterbot import main
from monsterbot.db import Database
from monsterbot.web import Runtime


class FakeClient:
    """Stands in for discord.Client: `outcome` is 'ready', or an exception raised by start()."""

    def __init__(self, outcome):
        self.outcome, self.user, self.closed = outcome, "TestBot#0001", False
        self._ready = asyncio.Event()

    async def start(self, token):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        self._ready.set()
        await asyncio.Event().wait()  # runs until cancelled/closed

    async def wait_until_ready(self):
        await self._ready.wait()

    async def close(self):
        self.closed = True


def test_run_bot_states(tmp_path, monkeypatch):
    outcomes = [discord.LoginFailure("bad"), "ready"]
    clients = []

    def create_client(db):
        clients.append(FakeClient(outcomes.pop(0)))
        return clients[-1]

    monkeypatch.setattr(main, "create_client", create_client)

    async def scenario():
        rt = Runtime(Database(tmp_path / "t.db"))
        task = asyncio.create_task(main.run_bot(rt))
        await asyncio.sleep(0.05)
        assert "No bot token" in rt.status and clients == []

        rt.db.set_setting("bot_token", "wrong")
        rt.restart.set()
        await asyncio.sleep(0.05)
        assert "rejected the token" in rt.status and clients[0].closed and rt.client is None

        rt.db.set_setting("bot_token", "right")
        rt.restart.set()
        await asyncio.sleep(0.05)
        assert rt.status == "Connected as TestBot#0001" and rt.client is clients[1]

        rt.restart.set()  # token changed again -> old client closed
        await asyncio.sleep(0.05)
        assert clients[1].closed
        task.cancel()

    asyncio.run(scenario())


def test_backup_once_keeps_latest(tmp_path):
    db = Database(tmp_path / "t.db")
    db.set_setting("x", "1")
    folder = tmp_path / "backups"
    for i in range(20):
        main.backup_once(db, folder, dt.date(2024, 1, 1) + dt.timedelta(days=i))
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == main.BACKUPS_TO_KEEP and files[-1] == "monsterbot-2024-01-20.db"
    assert Database(folder / files[-1]).get_setting("x") == "1"


def test_backup_ignores_leftover_partial_file(tmp_path):
    db = Database(tmp_path / "t.db")
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "monsterbot-2024-01-01.tmp").write_bytes(b"half written")
    target = main.backup_once(db, folder, dt.date(2024, 1, 1))
    assert Database(target).get_setting("x") is None and not list(folder.glob("*.tmp"))
