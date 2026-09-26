"""Discord gateway bot: slash commands and the daily report post."""
import logging
from typing import Literal

import discord
from discord import app_commands

from .db import Database
from .importer import ParsedFile

log = logging.getLogger(__name__)

Days = app_commands.Range[int, 1, 365]
Category = Literal["Above", "Below"]
Order = Literal["ascending", "descending"]
MESSAGE_LIMIT = 1900  # Discord allows 2000 characters, leave room for code fences
DISCORD_MAX_INT = 2**53 - 1  # largest integer option value Discord accepts


def compact(n) -> str:
    n = n or 0
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000 or unit == "B":
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.2f}{unit}"
        n /= 1000


def chunk_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    return chunks + ([current] if current else [])


async def send_list(interaction: discord.Interaction, header: str, lines: list[str]):
    chunks = chunk_lines(lines) or ["No players found."]
    blocks = [f"```\n{c}```" if lines else c for c in chunks]
    await interaction.followup.send(f"{header}\n{blocks[0]}")
    for block in blocks[1:]:
        await interaction.followup.send(block)


def filter_players(players, key, category: Category, value, order: Order):
    kept = [p for p in players if (p[key] >= value if category == "Above" else p[key] < value)]
    return sorted(kept, key=lambda p: p[key], reverse=order == "descending")


def player_embed(name: str, days: int, hunts: dict, kills: dict) -> discord.Embed:
    embed = discord.Embed(title=f"Lords Mobile history for {name}", description=f"Last {days} days")
    if not hunts or not hunts["days"]:
        embed.add_field(name="Hunting", value="No data", inline=False)
    else:
        embed.add_field(name="Date period", value=f"{hunts['from_day']} → {hunts['to_day']}", inline=False)
        levels = lambda kind: "\n".join(f"Lvl {i}: {hunts[f'l{i}_{kind}'] or 0}" for i in range(1, 6))
        for kind, label in (("hunt", "Hunting"), ("purchase", "Purchases")):
            count, points = hunts[kind] or 0, hunts[f"points_{kind}"] or 0
            value = (
                f"{label}: {count}\nPoints: {points}\nAvg points/day: {points / hunts['days']:.2f}\n{levels(kind)}"
                if count else f"Zero {label.lower()}"
            )
            embed.add_field(name=label, value=value)
    if kills and kills["days"]:
        embed.add_field(
            name="Kills", value=f"Gained: {kills['kills']:,}\nPeriod: {kills['from_day']} → {kills['to_day']}", inline=False
        )
    else:
        embed.add_field(name="Kills", value="Need guild list data", inline=False)
    return embed


def signed(n) -> str:
    return ("+" if (n or 0) > 0 else "") + compact(n)


def members_field(changes: dict) -> str:
    lines = [f"➕ Joined ({len(changes['joined'])}): {', '.join(changes['joined'])}" if changes["joined"] else "",
             f"➖ Left ({len(changes['left'])}): {', '.join(changes['left'])}" if changes["left"] else ""]
    return _truncate("\n".join(line for line in lines if line) or "No changes")


def report_embed(title: str, parsed: ParsedFile, changes: dict | None = None) -> discord.Embed:
    embed = discord.Embed(title=f"📊 {title} daily report: {parsed.day.isoformat()}")
    rows = parsed.rows
    if parsed.kind == "hunts":
        top = sorted((r for r in rows if r["hunt"]), key=lambda r: (r["hunt"], r["points_hunt"] or 0), reverse=True)[:5]
        embed.add_field(
            name="🏹 Top hunters",
            value="\n".join(f"{i}. {r['name']}: {r['hunt']} hunts" for i, r in enumerate(top, 1)) or "Nobody hunted",
            inline=False,
        )
        zero = sorted((r["name"] for r in rows if not r["hunt"]), key=str.lower)
        embed.add_field(name=f"⚠️ Zero hunts ({len(zero)})", value=_truncate(", ".join(zero)) or "None 🎉", inline=False)
    else:
        top = sorted((r for r in rows if (r["kills_diff"] or 0) > 0), key=lambda r: r["kills_diff"], reverse=True)[:5]
        embed.add_field(
            name="⚔️ Most kills gained",
            value="\n".join(f"{i}. {r['name']}: +{compact(r['kills_diff'])}" for i, r in enumerate(top, 1))
            or "No kills gained",
            inline=False,
        )
    if parsed.kind == "guild_list" and changes:
        embed.add_field(name=f"👥 Members since {changes['previous']}", value=members_field(changes), inline=False)
    embed.set_footer(text=f"{len(rows)} players")
    return embed


def _truncate(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def create_client(db: Database) -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True  # server + channel lists for the admin site; no privileged intents needed
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, db)

    synced = False

    @client.event
    async def on_ready():  # fires again after every reconnect
        nonlocal synced
        log.info("Connected to Discord as %s in %d server(s)", client.user, len(client.guilds))
        if synced:
            return
        await tree.sync()
        # Remove server-scoped commands left behind by the old Flask slash bot, so nothing shows twice.
        for guild in client.guilds:
            await tree.sync(guild=guild)
        synced = True  # only after success, so a failed sync is retried on the next reconnect

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("Command %s failed", interaction.command and interaction.command.qualified_name, exc_info=error)
        message = "Something went wrong, check the MonsterBot log."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return client


async def post_report(client: discord.Client | None, link: dict, parsed: ParsedFile, db: Database | None = None):
    if client is None or not client.is_ready():
        log.warning("Bot not connected, skipped report for link %s", link["id"])
        return
    channel = client.get_channel(link["channel_id"]) or await client.fetch_channel(link["channel_id"])
    guild = client.get_guild(link["discord_guild_id"])
    title = link["label"] or (guild.name if guild else "Guild")
    changes = db.membership_diff(link["discord_guild_id"], parsed.day.isoformat()) if db else None
    await channel.send(embed=report_embed(title, parsed, changes))


def register_commands(tree: app_commands.CommandTree, db: Database):
    player = app_commands.Group(name="player", description="Look up a player's stats", guild_only=True)
    hunts = app_commands.Group(name="hunts", description="Guild hunting stats", guild_only=True)
    purchases = app_commands.Group(name="purchases", description="Guild purchase stats", guild_only=True)
    kills = app_commands.Group(name="kills", description="Guild kill stats", guild_only=True)

    async def show_player(interaction: discord.Interaction, found: dict | None, days: int):
        if not found:
            await interaction.response.send_message("Unable to find data for that player.", ephemeral=True)
            return
        guild = interaction.guild_id
        name = found["name"] + (" (no longer in the guild exports)" if found.get("left") else "")
        embed = player_embed(
            name, days,
            db.player_hunt_totals(guild, found["user_id"], days), db.player_kills(guild, found["user_id"], days),
        )
        await interaction.response.send_message(embed=embed)

    @player.command(name="castlename", description="Show a player's stats by castle name")
    @app_commands.describe(castlename="Exact castle name", days="Days of history (default 30)")
    async def player_castlename(interaction: discord.Interaction, castlename: str, days: Days = 30):
        await show_player(interaction, db.find_player(interaction.guild_id, name=castlename), days)

    @player.command(name="discordname", description="Show a player's stats by Discord member (needs /player link)")
    @app_commands.describe(user="Discord member", days="Days of history (default 30)")
    async def player_discordname(interaction: discord.Interaction, user: discord.Member, days: Days = 30):
        igg = db.igg_for_user(user.id, interaction.guild_id)
        if igg is None:
            await interaction.response.send_message(f"{user.display_name} hasn't linked an IGG ID.", ephemeral=True)
            return
        await show_player(interaction, db.find_player(interaction.guild_id, user_id=igg), days)

    @player.command(name="search", description="Find players by part of their name")
    async def player_search(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
        await interaction.response.defer()
        found = db.search_players(interaction.guild_id, name)
        await send_list(interaction, f"Players matching '{name}':", [f"IGG: {p['user_id']}, Name: {p['name']}" for p in found])

    @player.command(name="link", description="Link your IGG ID to your Discord account")
    async def player_link(interaction: discord.Interaction, igg_id: app_commands.Range[int, 1, DISCORD_MAX_INT]):
        db.link_user(interaction.user.id, interaction.guild_id, igg_id)
        await interaction.response.send_message(f"Your IGG ID {igg_id} is now linked.", ephemeral=True)

    def points_command(group: app_commands.Group, kind: str, label: str):
        @group.command(name="query", description=f"Players whose average {label} points/day are above or below a value")
        @app_commands.describe(category="Above or below the value", value="Average points per day",
                               order="Sort order", days="Days of history (default 30)")
        async def query(interaction: discord.Interaction, category: Category, value: int,
                        order: Order = "ascending", days: Days = 30):
            await interaction.response.defer()
            players = filter_players(db.points_per_day(interaction.guild_id, days, kind), "average", category, value, order)
            lines = [f"Avg points/day: {p['average']:.2f}, Name: {p['name']}" for p in players]
            await send_list(interaction, f"{label.title()}: average {category.lower()} {value} over {days} days", lines)

    points_command(hunts, "hunt", "hunting")
    points_command(purchases, "purchase", "purchase")

    @kills.command(name="query", description="Players whose kills gained are above or below a value")
    @app_commands.describe(category="Above or below the value", value="Kills gained",
                           order="Sort order", days="Days of history (default 30)")
    async def kills_query(interaction: discord.Interaction, category: Category, value: int,
                          order: Order = "ascending", days: Days = 30):
        await interaction.response.defer()
        players = filter_players(db.kills_gained(interaction.guild_id, days), "kills", category, value, order)
        lines = [f"Kills: {compact(p['kills']):>8}, Name: {p['name']}" for p in players]
        await send_list(interaction, f"Kills gained {category.lower()} {compact(value)} over {days} days", lines)

    @kills.command(name="total", description="Total kills gained by the guild")
    async def kills_total(interaction: discord.Interaction, days: Days = 30):
        total = sum(p["kills"] or 0 for p in db.kills_gained(interaction.guild_id, days))
        await interaction.response.send_message(f"Guild kills gained over the past {days} days: **{compact(total)}**")

    guild_group, might = register_guild_and_might(db)
    for group in (player, hunts, purchases, kills, guild_group, might):
        tree.add_command(group)


def register_guild_and_might(db: Database):
    guild_group = app_commands.Group(name="guild", description="Guild members and goals", guild_only=True)
    might = app_commands.Group(name="might", description="Guild might stats", guild_only=True)

    @guild_group.command(name="changes", description="Who joined and who left the guild")
    @app_commands.describe(days="Days of history (default 7)")
    async def guild_changes(interaction: discord.Interaction, days: Days = 7):
        await interaction.response.defer()
        lines = []
        for change in db.member_changes(interaction.guild_id, days):
            lines += [f"{change['day']}  + {name}" for name in change["joined"]]
            lines += [f"{change['day']}  - {name}" for name in change["left"]]
        header = f"Members who joined (+) or left (-) in the past {days} days:"
        await send_list(interaction, header, lines or ["No changes (or not enough guild list exports yet)."])

    @guild_group.command(name="goals", description="Players below their hunt or purchase goal")
    @app_commands.describe(days="Days to average (default 7)", type="Hunt or purchase goal",
                           below="Show players below this % of the goal (default 100)")
    async def guild_goals(interaction: discord.Interaction, days: Days = 7,
                          type: Literal["hunt", "purchase"] = "hunt", below: app_commands.Range[int, 1, 1000] = 100):
        await interaction.response.defer()
        players = db.goal_averages(interaction.guild_id, days, type)
        if not players or not any(p["pct"] for p in players):
            await interaction.followup.send(f"No {type} goal data. Is a {type} goal set in LordsBot?")
            return
        behind = sorted((p for p in players if p["pct"] * 100 < below), key=lambda p: p["pct"])
        lines = [f"Goal: {p['pct'] * 100:5.0f}%, Name: {p['name']}" for p in behind]
        await send_list(interaction, f"{type.title()} goal below {below}% (average over {days} days):", lines)

    @might.command(name="top", description="Strongest players in the guild right now")
    async def might_top(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 10):
        await interaction.response.defer()
        players = db.might_top(interaction.guild_id, count)
        lines = [f"{i:>3}. Might: {compact(p['might']):>8}, Name: {p['name']}" for i, p in enumerate(players, 1)]
        await send_list(interaction, f"Top {count} by might:", lines)

    @might.command(name="growth", description="Might gained (or lost) per player")
    @app_commands.describe(days="Days of history (default 7)", order="Biggest gains first (descending) or losses first",
                           count="How many players to show (default 25)")
    async def might_growth(interaction: discord.Interaction, days: Days = 7, order: Order = "descending",
                           count: app_commands.Range[int, 1, 100] = 25):
        await interaction.response.defer()
        players = sorted(db.might_growth(interaction.guild_id, days), key=lambda p: p["growth"] or 0,
                         reverse=order == "descending")[:count]
        lines = [f"Might: {signed(p['growth']):>9}, Name: {p['name']}" for p in players]
        await send_list(interaction, f"Might growth over {days} days:", lines)

    return guild_group, might
