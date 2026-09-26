"""Data page: one-off folder import and database clean-up."""
import asyncio
import datetime as dt
import logging
from pathlib import Path

from aiohttp import web

from .importer import discover_folders, import_folder
from .web import DEFAULT_ROOT, RT, Runtime, e, page, redirect

log = logging.getLogger(__name__)
CONFIRM_WORD = "DELETE"


def server_name(rt: Runtime, guild_id: int) -> str:
    guild = rt.client.get_guild(guild_id) if rt.client else None
    return guild.name if guild else f"Server {guild_id}"


def server_options(rt: Runtime, include_all: bool) -> str:
    """Servers the bot is in, plus any server that still has data (the bot may have been removed)."""
    ids = {g.id for g in rt.client.guilds} if rt.ready else set()
    ids |= {r["discord_guild_id"] for r in rt.db.data_summary()}
    options = sorted((server_name(rt, i), i) for i in ids)
    all_option = '<option value="all">All servers</option>' if include_all else ""
    return all_option + "".join(f'<option value="{i}">{e(name)}</option>' for name, i in options)


def parse_server(value: str) -> int | None:
    """'all' -> None (every server), otherwise a server id."""
    return None if value == "all" else int(value)


async def data_page(request: web.Request):
    rt: Runtime = request.app[RT]
    summary = rt.db.data_summary()
    rows = "".join(
        f"<tr><td>{e(server_name(rt, r['discord_guild_id']))}</td><td>{'Hunts' if r['kind'] == 'hunts' else 'Guild list'}</td>"
        f"<td>{r['players']}</td><td>{r['rows']}</td><td>{e(r['from_day'])} → {e(r['to_day'])}</td></tr>"
        for r in summary
    )
    table = (
        "<table><tr><th>Server</th><th>Data</th><th>Players</th><th>Rows</th><th>Days</th></tr>"
        f"{rows}</table>" if rows else "<p>No data yet.</p>"
    )
    running = rt.import_task is not None and not rt.import_task.done()
    root = Path(rt.db.get_setting("root_folder", DEFAULT_ROOT))
    folders = await asyncio.to_thread(discover_folders, root)
    datalist = "".join(f'<option value="{e(f)}">' for f in [root, *folders])
    servers = server_options(rt, include_all=False)
    if running:
        import_form = '<p class="notice">A folder import is running. Refresh this page to see when it is done.</p>'
    elif not servers:
        import_form = '<p class="bad">Connect the bot on the <a href="/setup">Setup</a> page first.</p>'
    else:
        import_form = f"""<form method="post" action="/data/import">
<label>Folder with exports<input name="folder" list="folders" required placeholder="C:\\LordsBot\\...\\stats\\exported"></label>
<datalist id="folders">{datalist}</datalist>
<label class="check"><input type="checkbox" name="recursive"> Include sub-folders</label>
<label>Discord server the data belongs to<select name="server" required>{servers}</select></label>
<button>Import folder</button></form>"""
    confirm = (f'<label>Type {CONFIRM_WORD} to confirm<input name="confirm" required autocomplete="off" '
               f'pattern="{CONFIRM_WORD}"></label>')
    body = f"""
<section><h2>What's stored</h2>{table}</section>
<section><h2>Import a folder</h2>
<p class="muted">Linked folders already import everything in them automatically. Use this for exports kept
somewhere else, such as an archive of older files. Nothing is posted to Discord, and files that were
imported before are simply updated.</p>{import_form}</section>
<section><h2>Delete old data</h2>
<p class="muted">Removes stats older than the number of days you choose. A backup is saved to the
<code>backups</code> folder first.</p>
<form method="post" action="/data/delete-old">
<label>Delete everything older than (days)<input type="number" name="days" min="1" max="3650" value="365" required></label>
<label>Server<select name="server">{server_options(rt, include_all=True)}</select></label>
{confirm}<button class="danger">Delete old data</button></form></section>
<section><h2>Delete all data</h2>
<p class="muted">Removes all hunt and guild-list stats. Settings, links and linked IGG IDs are kept.
A backup is saved to the <code>backups</code> folder first.</p>
<form method="post" action="/data/delete-all">
<label>Server<select name="server">{server_options(rt, include_all=True)}</select></label>
<label class="check"><input type="checkbox" name="reimport" checked> Import the files in linked folders again
afterwards (start fresh from the exports)</label>
{confirm}<button class="danger">Delete all data</button></form></section>"""
    return page("Data", body, request.query.get("msg"))


async def data_import(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    folder = Path(form.get("folder", "").strip())
    if not str(folder) or not folder.is_dir():
        redirect("/data", f"Folder not found: {folder}")
    try:
        guild = int(form.get("server", ""))
    except ValueError:
        redirect("/data", "Pick a Discord server.")
    if rt.import_task is not None and not rt.import_task.done():
        redirect("/data", "A folder import is already running.")

    async def run():
        try:
            await asyncio.to_thread(import_folder, rt.db, folder, guild, bool(form.get("recursive")))
        except Exception:
            log.exception("Folder import failed: %s", folder)

    rt.import_task = asyncio.create_task(run())
    redirect("/activity", f"Importing {folder}. Files appear below as they are imported; refresh to follow along.")


def _confirmed_server(form) -> int | None:
    if form.get("confirm", "").strip() != CONFIRM_WORD:
        redirect("/data", f"Nothing deleted: type {CONFIRM_WORD} to confirm.")
    try:
        return parse_server(form.get("server", "all"))
    except ValueError:
        redirect("/data", "Pick a server.")


async def _safety_backup(rt: Runtime) -> str:
    if rt.backup_dir is None:
        return ""
    rt.backup_dir.mkdir(parents=True, exist_ok=True)
    target = rt.backup_dir / f"before-cleanup-{dt.datetime.now():%Y-%m-%d-%H%M%S}.db"
    await asyncio.to_thread(rt.db.backup, target)
    return f" Backup saved as backups\\{target.name}."


async def data_delete_old(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    guild = _confirmed_server(form)
    try:
        days = int(form.get("days", ""))
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        redirect("/data", "Days must be a whole number from 1 to 3650.")
    note = await _safety_backup(rt)
    deleted = await asyncio.to_thread(rt.db.delete_older_than, days, guild)
    log.info("Deleted %s rows older than %s days (server %s)", deleted, days, guild or "all")
    redirect("/data", f"Deleted {deleted} rows older than {days} days.{note}")


async def data_delete_all(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    guild = _confirmed_server(form)
    reimport = bool(form.get("reimport"))
    note = await _safety_backup(rt)
    deleted = await asyncio.to_thread(rt.db.delete_all_data, guild, reimport)
    log.info("Deleted all %s rows (server %s, reimport=%s)", deleted, guild or "all", reimport)
    again = " Linked folders will be imported again within a minute." if reimport else ""
    redirect("/data", f"Deleted {deleted} rows.{again}{note}")


def routes():
    return [
        web.get("/data", data_page),
        web.post("/data/import", data_import),
        web.post("/data/delete-old", data_delete_old),
        web.post("/data/delete-all", data_delete_all),
    ]
