"""MonsterBot entry point: admin website + Discord bot + xlsx importer + nightly backups, on one event loop."""
import asyncio
import datetime as dt
import logging
import os
import sys
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from aiohttp import web

from . import __version__, importer
from .bot import create_client, post_report
from .db import Database
from .web import Runtime, create_app

log = logging.getLogger("monsterbot")
BACKUPS_TO_KEEP = 14
BOT_RETRY_SECONDS = 60


def home_dir() -> Path:
    """Data lives next to MonsterBot.exe (or in MONSTERBOT_HOME / the current folder when run from source)."""
    if "MONSTERBOT_HOME" in os.environ:
        return Path(os.environ["MONSTERBOT_HOME"])
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()


def setup_logging(home: Path):
    (home / "logs").mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(home / "logs" / "monsterbot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
                logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


async def run_bot(rt: Runtime):
    """Keep one Discord client running; restart it when the token changes on the Setup page."""
    while True:
        rt.restart.clear()
        token = rt.db.get_setting("bot_token")
        if not token:
            rt.status = "No bot token yet. Paste it below."
            await rt.restart.wait()
            continue
        rt.status = "Connecting…"
        client = rt.client = create_client(rt.db)
        client_task = asyncio.create_task(client.start(token))
        restart_task = asyncio.create_task(rt.restart.wait())
        ready_task = asyncio.create_task(client.wait_until_ready())
        await asyncio.wait({client_task, restart_task, ready_task}, return_when=asyncio.FIRST_COMPLETED)
        if ready_task.done() and not client_task.done():
            rt.status = f"Connected as {client.user}"
            await asyncio.wait({client_task, restart_task}, return_when=asyncio.FIRST_COMPLETED)
        ready_task.cancel()
        retry = None
        if client_task.done():
            error = client_task.exception()
            if isinstance(error, discord.LoginFailure):
                rt.status = "Discord rejected the token. Copy it again from the Developer Portal."
            else:
                rt.status = f"Disconnected ({type(error).__name__ if error else 'closed'}), retrying in {BOT_RETRY_SECONDS}s"
                retry = BOT_RETRY_SECONDS
            log.error("Discord client stopped: %s", rt.status, exc_info=error)
        await client.close()
        await asyncio.gather(client_task, return_exceptions=True)
        rt.client = None
        if not restart_task.done():
            try:
                await asyncio.wait_for(restart_task, timeout=retry)  # None = wait for a new token
            except asyncio.TimeoutError:
                pass
        restart_task.cancel()


def backup_once(db: Database, folder: Path, today: dt.date | None = None) -> Path:
    today = today or dt.date.today()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"monsterbot-{today.isoformat()}.db"
    if not target.exists():
        partial = target.with_suffix(".tmp")  # a crash mid-backup must not leave a "done" file behind
        partial.unlink(missing_ok=True)
        db.backup(partial)
        partial.replace(target)
    for old in sorted(folder.glob("monsterbot-*.db"))[:-BACKUPS_TO_KEEP]:
        old.unlink()
    return target


async def backup_forever(db: Database, folder: Path):
    while True:
        try:
            await asyncio.to_thread(backup_once, db, folder)
        except Exception:
            log.exception("Backup failed")
        await asyncio.sleep(3600)  # one file per day; hourly check covers PCs that sleep or restart


async def main():
    home = home_dir()
    setup_logging(home)
    port = int(os.environ.get("MONSTERBOT_PORT", "8080"))
    url = f"http://127.0.0.1:{port}/"
    log.info("MonsterBot %s starting, data folder %s", __version__, home)

    db = Database(home / "monsterbot.db")
    rt = Runtime(db, port, backup_dir=home / "backups")
    runner = web.AppRunner(create_app(rt))
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except OSError:
        log.error("Port %s is in use. Is MonsterBot already running? Opening %s", port, url)
        webbrowser.open(url)
        return
    log.info("Admin website: %s", url)
    if not db.get_setting("bot_token"):
        webbrowser.open(url + "setup")

    async def on_imported(link, parsed):
        await post_report(rt.client, link, parsed)

    try:
        await asyncio.gather(run_bot(rt), importer.run_forever(db, on_imported), backup_forever(db, home / "backups"))
    finally:
        await runner.cleanup()


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
