"""Local admin website (http://127.0.0.1:8080): bot setup, folder -> Discord links, import activity."""
import asyncio
import html
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import discord
from aiohttp import web

from . import autostart, picker
from .db import MANUAL_IMPORT, Database
from .importer import discover_castles, import_file

DEFAULT_ROOT = r"C:\LordsBot\config"
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)


@dataclass
class Runtime:
    db: Database
    port: int = 8080
    client: discord.Client | None = None
    status: str = "Not started"
    restart: asyncio.Event = field(default_factory=asyncio.Event)
    backup_dir: Path | None = None  # safety backup before clean-ups
    import_task: asyncio.Task | None = None  # running folder import (Data page)

    @property
    def ready(self) -> bool:
        return self.client is not None and self.client.is_ready()


RT = web.AppKey("rt", Runtime)


e = lambda value: html.escape(str(value if value is not None else ""))


def page(title: str, body: str, msg: str | None = None) -> web.Response:
    notice = f'<p class="notice">{e(msg)}</p>' if msg else ""
    return web.Response(content_type="text/html", text=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MonsterBot · {e(title)}</title><style>{CSS}</style></head>
<body><header><strong>MonsterBot</strong><nav>
<a href="/setup">Setup</a><a href="/links">Links</a><a href="/activity">Activity</a><a href="/data">Data</a></nav></header>
<main><h1>{e(title)}</h1>{notice}{body}</main></body></html>""")


def redirect(path: str, msg: str | None = None):
    raise web.HTTPSeeOther(path + (f"?msg={quote(msg)}" if msg else ""))


@web.middleware
async def local_only(request: web.Request, handler):
    """Block DNS-rebinding and cross-site form posts: only this machine's own pages may talk to us."""
    port = request.app[RT].port
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if request.host not in allowed:
        raise web.HTTPForbidden(text="MonsterBot only accepts requests to 127.0.0.1")
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin and origin.removeprefix("http://") not in allowed:
        raise web.HTTPForbidden(text="Cross-site request blocked")
    return await handler(request)


# --- Setup ---------------------------------------------------------------------------------------
async def setup_page(request: web.Request):
    rt: Runtime = request.app[RT]
    db = rt.db
    has_token = bool(db.get_setting("bot_token"))
    status_class = "ok" if rt.ready else "bad"
    bot_info = ""
    if rt.ready:
        invite = discord.utils.oauth_url(
            rt.client.application_id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands")
        )
        servers = "".join(f"<li>{e(g.name)}</li>" for g in rt.client.guilds) or "<li>None yet, invite the bot</li>"
        bot_info = f"""<p>Servers the bot is in:</p><ul>{servers}</ul>
<p><a class="button" href="{e(invite)}" target="_blank" rel="noopener">Invite bot to a server</a></p>"""
    if autostart.supported():
        on = autostart.is_enabled()
        auto = f"""<form method="post" action="/autostart"><input type="hidden" name="enable" value="{int(not on)}">
<p>Start with Windows: <strong>{"on" if on else "off"}</strong> <button>{"Turn off" if on else "Turn on"}</button></p></form>"""
    else:
        auto = "<p class=muted>Start with Windows is available in the packaged MonsterBot.exe.</p>"
    root = Path(db.get_setting("root_folder", DEFAULT_ROOT))
    castles = await asyncio.to_thread(discover_castles, root)
    if castles:
        found = f'<p class="ok">Found {len(castles)} castle(s): {e(", ".join(castles))}</p>'
    else:
        found = ('<p class="bad">No castle folders found here. Choose the LordsBot <strong>config</strong> folder, '
                 'the one that has a folder per castle IGG ID (usually C:\\LordsBot\\config).</p>')
    browse = (
        '<button formaction="/setup/browse" formnovalidate>Browse…</button>' if picker.supported() else ""
    )
    body = f"""
<section><h2>Status</h2><p class="{status_class}">{e(rt.status)}</p>{bot_info}{auto}</section>
<section><h2>Settings</h2><form method="post" action="/setup">
<label>Discord bot token<input type="password" name="token" autocomplete="off"
 placeholder="{"Saved. Paste a new token to replace it" if has_token else "Paste your bot token"}"></label>
<label>LordsBot config folder<input name="root" value="{e(root)}"></label>
<p class="muted">The folder with one sub-folder per castle IGG ID, e.g. <code>C:\\LordsBot\\config</code>.
Exports are read from <code>&lt;IGG&gt;\\stats\\exported</code> inside it.</p>{found}{browse}
<label>Hide players missing from the exports for this many days (they probably left the guild; 0 = never hide)
<input type="number" name="inactive_days" min="0" max="365" value="{db.inactive_days()}"></label>
<button>Save</button></form>
<p class="muted">Don't have a bot yet? Follow "Create your Discord bot" in the README.</p></section>"""
    return page("Setup", body, request.query.get("msg"))


async def setup_save(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    token = form.get("token", "").strip()
    root = form.get("root", "").strip()
    if root:
        if not Path(root).is_dir():
            redirect("/setup", f"Folder not found: {root}")
        rt.db.set_setting("root_folder", root)
    inactive = form.get("inactive_days", "").strip()
    if inactive:
        if not inactive.isdigit() or int(inactive) > 365:
            redirect("/setup", "Days must be a whole number from 0 to 365.")
        rt.db.set_setting("inactive_days", str(int(inactive)))
    if token:
        rt.db.set_setting("bot_token", token)
        rt.status = "Connecting…"
        rt.restart.set()
    redirect("/setup", "Saved." + (" Connecting to Discord, refresh in a few seconds." if token else ""))


async def setup_browse(request: web.Request):
    rt: Runtime = request.app[RT]
    current = rt.db.get_setting("root_folder", DEFAULT_ROOT)
    chosen = await asyncio.to_thread(picker.pick_folder, current)
    if not chosen:
        redirect("/setup", "No folder chosen.")
    rt.db.set_setting("root_folder", chosen)
    castles = await asyncio.to_thread(discover_castles, Path(chosen))
    redirect("/setup", f"LordsBot folder set to {chosen}. Found {len(castles)} castle(s).")


async def autostart_save(request: web.Request):
    form = await request.post()
    autostart.set_enabled(form.get("enable") == "1")
    redirect("/setup")


# --- Links ---------------------------------------------------------------------------------------
def channel_options(client: discord.Client) -> str:
    groups = []
    for guild in sorted(client.guilds, key=lambda g: g.name.lower()):
        options = [f'<option value="{guild.id}:">{e(guild.name)}: import only, no posts</option>']
        options += [
            f'<option value="{guild.id}:{ch.id}">#{e(ch.name)}</option>'
            for ch in guild.text_channels
            if ch.permissions_for(guild.me).send_messages
        ]
        groups.append(f'<optgroup label="{e(guild.name)}">{"".join(options)}</optgroup>')
    return "".join(groups)


def describe_target(client: discord.Client | None, link: dict) -> tuple[str, str]:
    guild = client.get_guild(link["discord_guild_id"]) if client else None
    channel = guild.get_channel(link["channel_id"]) if guild and link["channel_id"] else None
    guild_name = guild.name if guild else f"Server {link['discord_guild_id']}"
    channel_name = f"#{channel.name}" if channel else ("—" if not link["channel_id"] else f"Channel {link['channel_id']}")
    return guild_name, channel_name


async def links_page(request: web.Request):
    rt: Runtime = request.app[RT]
    rows = []
    for link in rt.db.links():
        guild_name, channel_name = describe_target(rt.client, link)
        actions = "".join(
            f'<button name="action" value="{a}">{label}</button>'
            for a, label in (("enabled", "Disable" if link["enabled"] else "Enable"),
                             ("post_report", "Stop report" if link["post_report"] else "Post report"),
                             ("delete", "Delete"))
        )
        igg = Path(link["folder"]).parent.parent.name
        rows.append(f"""<tr><td>{e(link["label"])}</td><td title="{e(link["folder"])}">{e(castle_label(rt.db, igg))}</td>
<td>{e(guild_name)}</td><td>{e(channel_name)}</td><td>{"yes" if link["post_report"] else "no"}</td>
<td>{"yes" if link["enabled"] else "no"}</td>
<td><form method="post" action="/links/{link["id"]}">{actions}</form></td></tr>""")
    table = (
        "<table><tr><th>Guild</th><th>Castle</th><th>Discord server</th><th>Channel</th><th>Daily report</th>"
        f"<th>Enabled</th><th></th></tr>{''.join(rows)}</table>" if rows else "<p>No links yet.</p>"
    )
    if rt.ready:
        root = Path(rt.db.get_setting("root_folder", DEFAULT_ROOT))
        castles = await asyncio.to_thread(discover_castles, root)
        options = "".join(f'<option value="{igg}">{e(castle_label(rt.db, igg))}</option>' for igg in castles)
        found = (f"{len(castles)} castle(s) found in {e(root)}." if castles
                 else f'No castles found in {e(root)}. Check the folder on <a href="/setup">Setup</a>.')
        add = f"""<form method="post" action="/links">
<label>Castle IGG ID<input name="igg" list="castles" required inputmode="numeric" pattern="[0-9]+"
 placeholder="e.g. 123456789"></label>
<datalist id="castles">{options}</datalist><p class="muted">{found} Pick one from the list or type the IGG ID.</p>
<label>Guild name (shown in reports)<input name="label" maxlength="50" placeholder="-R-"></label>
<label>Discord server and channel<select name="target" required>{channel_options(rt.client)}</select></label>
<label class="check"><input type="checkbox" name="post_report" checked> Post the daily report to this channel</label>
<button>Add link</button></form>"""
    else:
        add = '<p class="bad">Connect the bot on the <a href="/setup">Setup</a> page first, so servers and channels can be listed.</p>'
    return page("Links", f"<section>{table}</section><section><h2>Add a link</h2>{add}</section>", request.query.get("msg"))


def parse_target(value: str) -> tuple[int, int | None]:
    guild, _, channel = value.partition(":")
    return int(guild), int(channel) if channel else None


async def links_add(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    igg = form.get("igg", "").strip()
    root = Path(rt.db.get_setting("root_folder", DEFAULT_ROOT))
    folder = (await asyncio.to_thread(discover_castles, root)).get(igg)
    if folder is None:
        redirect("/links", f"No export folder for IGG ID {igg} in {root}. Check the LordsBot folder on Setup.")
    try:
        guild_id, channel_id = parse_target(form.get("target", ""))
    except ValueError:
        redirect("/links", "Pick a Discord server and channel.")
    guild = rt.client.get_guild(guild_id) if rt.ready else None
    if guild is None or (channel_id is not None and guild.get_channel(channel_id) is None):
        redirect("/links", "That server or channel isn't available to the bot.")
    post_report = bool(form.get("post_report")) and channel_id is not None
    rt.db.add_link(str(folder), form.get("label", "").strip()[:50], guild_id, channel_id, post_report)
    redirect("/links", "Link added. Existing files in the folder are imported within a minute.")


def castle_label(db: Database, igg: str) -> str:
    name = db.castle_name(igg) if igg.isdigit() else None
    return f"{igg} ({name})" if name else igg


async def links_action(request: web.Request):
    rt: Runtime = request.app[RT]
    link_id = int(request.match_info["id"])
    link = next((l for l in rt.db.links() if l["id"] == link_id), None)
    if link is None:
        redirect("/links", "Link not found.")
    action = (await request.post()).get("action")
    if action == "delete":
        rt.db.delete_link(link_id)
    elif action == "enabled":
        rt.db.update_link(link_id, enabled=int(not link["enabled"]))
    elif action == "post_report":
        if not link["channel_id"]:
            redirect("/links", "This link has no channel. Delete it and add it again with a channel.")
        rt.db.update_link(link_id, post_report=int(not link["post_report"]))
    redirect("/links")


# --- Activity ------------------------------------------------------------------------------------
async def activity_page(request: web.Request):
    rt: Runtime = request.app[RT]
    rows = "".join(
        f"""<tr><td>{e(r["imported_at"])}</td><td>{e(r["label"] if r["link_id"] != MANUAL_IMPORT else "Folder import")}</td><td class="path">{e(Path(r["path"]).name)}</td>
<td>{e(r["kind"] or "")}</td><td>{e(r["day"] or "")}</td><td>{r["rows"]}</td><td class="bad">{e(r["error"] or "")}</td>
<td><form method="post" action="/activity/reimport"><input type="hidden" name="path" value="{e(r["path"])}">
<input type="hidden" name="link_id" value="{r["link_id"]}"><button>Re-import</button></form></td></tr>"""
        for r in rt.db.recent_imports()
    )
    body = (
        "<table><tr><th>When</th><th>Guild</th><th>File</th><th>Type</th><th>Day</th><th>Rows</th><th>Error</th><th></th></tr>"
        f"{rows}</table>" if rows else "<p>Nothing imported yet.</p>"
    )
    return page("Activity", f"<section>{body}</section>", request.query.get("msg"))


async def activity_reimport(request: web.Request):
    rt: Runtime = request.app[RT]
    form = await request.post()
    try:
        path, link_id = form["path"], int(form["link_id"])
    except (KeyError, ValueError):
        redirect("/activity", "Invalid request.")
    record = rt.db.imported_file(path, link_id)
    if record is None:
        redirect("/activity", "Unknown file.")
    if link_id == MANUAL_IMPORT:  # not in a linked folder, so the poller won't pick it up: import now
        if not Path(path).is_file():
            redirect("/activity", f"File no longer exists: {path}")
        manual = {"id": MANUAL_IMPORT, "discord_guild_id": record["discord_guild_id"]}
        parsed = await asyncio.to_thread(import_file, rt.db, manual, Path(path))
        redirect("/activity", "Re-imported." if parsed else "Re-import failed, see the error column.")
    rt.db.forget_import(path, link_id)
    redirect("/activity", "Queued, it will be re-imported within a minute.")


async def index(request: web.Request):
    rt: Runtime = request.app[RT]
    redirect("/links" if rt.db.get_setting("bot_token") else "/setup")


def create_app(rt: Runtime) -> web.Application:
    app = web.Application(middlewares=[local_only], client_max_size=64 * 1024)
    app[RT] = rt
    from . import data_page  # imports helpers from this module

    app.add_routes([
        web.get("/", index),
        web.get("/setup", setup_page), web.post("/setup", setup_save),
        web.post("/setup/browse", setup_browse), web.post("/autostart", autostart_save),
        web.get("/links", links_page), web.post("/links", links_add), web.post(r"/links/{id:\d+}", links_action),
        web.get("/activity", activity_page), web.post("/activity/reimport", activity_reimport),
        *data_page.routes(),
    ])
    return app


CSS = """
:root { --bg:#f6f7f9; --fg:#1d2330; --card:#fff; --line:#dde1e8; --muted:#667085; --accent:#5865f2; --ok:#1a7f37; --bad:#c62828; }
@media (prefers-color-scheme: dark) { :root { --bg:#15171c; --fg:#e6e8ee; --card:#1e2128; --line:#333845; --muted:#9aa1ae; --accent:#7b86ff; --ok:#4ac26b; --bad:#ff6b6b; } }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header { display:flex; gap:24px; align-items:center; padding:12px 16px; background:var(--card); border-bottom:1px solid var(--line); }
nav { display:flex; gap:16px; } a { color:var(--accent); }
main { max-width:1100px; margin:0 auto; padding:16px; }
section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:16px; overflow-x:auto; }
h1 { font-size:22px; } h2 { font-size:17px; margin-top:0; }
label { display:block; margin:0 0 12px; font-weight:600; }
label.check { font-weight:normal; }
input:not([type=checkbox]), select { display:block; width:100%; max-width:560px; margin-top:4px; padding:8px; font:inherit;
  color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:6px; }
button, .button { display:inline-block; padding:6px 12px; font:inherit; color:#fff; background:var(--accent); border:0;
  border-radius:6px; cursor:pointer; text-decoration:none; margin:2px; }
table { border-collapse:collapse; width:100%; } th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
.path { word-break:break-all; font-size:13px; } .muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); }
.danger { background:var(--bad); }
.notice { padding:8px 12px; border-left:4px solid var(--accent); background:var(--card); }
"""
