"""Guild bank balances, read live from LordsBot's banksettings.json (one per castle, not encrypted)."""
import json
import logging
import time
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands

from .db import Database

log = logging.getLogger(__name__)

RESOURCES = ("food", "stone", "wood", "ore", "gold")  # order of accountBalance in banksettings.json
Resource = Literal["total", "food", "stone", "wood", "ore", "gold"]


class BankUnavailable(Exception):
    pass


def read_bank(castle_dir: Path) -> list[dict] | None:
    """Accounts in one castle's bank, or None when that castle has no bank (file missing or bank disabled)."""
    path = castle_dir / "banksettings.json"
    if not path.is_file():
        return None
    for attempt in range(2):  # LordsBot rewrites the file while running; retry once on a half-written read
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            break
        except (json.JSONDecodeError, OSError):
            if attempt:
                raise BankUnavailable(f"could not read {path}")
            time.sleep(0.5)
    if not data.get("enableBank"):
        return None
    accounts = []
    for entry in data.get("accountData", []):
        balance = entry.get("accountBalance")
        if not isinstance(entry.get("UserID"), int) or not isinstance(balance, list) or len(balance) != len(RESOURCES):
            continue  # skip anything that doesn't look like a normal account
        accounts.append({"user_id": entry["UserID"], "name": str(entry.get("AccountName", "")),
                         **{r: int(v or 0) for r, v in zip(RESOURCES, balance)}})
    return accounts


def guild_balances(db: Database, discord_guild_id: int) -> tuple[list[dict], int]:
    """Balances per player across every bank castle linked to this Discord server, and how many banks were read."""
    castle_dirs = {Path(l["folder"]).parent.parent for l in db.links() if l["discord_guild_id"] == discord_guild_id}
    merged, banks = {}, 0
    for castle_dir in sorted(castle_dirs):
        accounts = read_bank(castle_dir)
        if accounts is None:
            continue
        banks += 1
        for a in accounts:
            total = merged.setdefault(a["user_id"], {"user_id": a["user_id"], "name": a["name"], **dict.fromkeys(RESOURCES, 0)})
            for r in RESOURCES:
                total[r] += a[r]
    for account in merged.values():
        account["total"] = sum(account[r] for r in RESOURCES)
    return list(merged.values()), banks


def balance_embed(account: dict, banks: int) -> discord.Embed:
    from .bot import compact

    embed = discord.Embed(title=f"Bank balance for {account['name']}")
    for r in RESOURCES:
        embed.add_field(name=r.title(), value=f"{compact(account[r])}\n`{account[r]:,}`")
    embed.set_footer(text=f"Live from {banks} LordsBot bank{'s' if banks != 1 else ''}")
    return embed


def register_bank(db: Database) -> app_commands.Group:
    from .bot import compact, send_list

    bank = app_commands.Group(name="bank", description="Guild bank balances from LordsBot", guild_only=True)

    async def load(interaction: discord.Interaction):
        try:
            accounts, banks = guild_balances(db, interaction.guild_id)
        except BankUnavailable:
            log.warning("Bank file busy for server %s", interaction.guild_id, exc_info=True)
            await interaction.response.send_message("The bank is being updated by LordsBot, try again in a moment.",
                                                    ephemeral=True)
            return None
        if not banks:
            await interaction.response.send_message(
                "No bank found. Link the bank castle to this server on the MonsterBot Links page, "
                "and check the bank is enabled in LordsBot.", ephemeral=True)
            return None
        return accounts, banks

    async def show(interaction: discord.Interaction, user_id: int | None, name: str | None):
        loaded = await load(interaction)
        if loaded is None:
            return
        accounts, banks = loaded
        account = next((a for a in accounts if a["user_id"] == user_id), None) if user_id is not None else None
        if account is None and name:
            account = next((a for a in accounts if a["name"].lower() == name.lower()), None)
        if account is None:
            await interaction.response.send_message("No bank account found for that player.", ephemeral=True)
            return
        await interaction.response.send_message(embed=balance_embed(account, banks))

    @bank.command(name="player", description="A player's bank balance by castle name")
    @app_commands.describe(castlename="Castle name")
    async def bank_player(interaction: discord.Interaction, castlename: str):
        found = db.find_player(interaction.guild_id, name=castlename)
        await show(interaction, found["user_id"] if found else None, castlename)

    @bank.command(name="me", description="Your own bank balance (needs /player link)")
    async def bank_me(interaction: discord.Interaction):
        igg = db.igg_for_user(interaction.user.id, interaction.guild_id)
        if igg is None:
            await interaction.response.send_message("Link your IGG ID first with /player link.", ephemeral=True)
            return
        await show(interaction, igg, None)

    @bank.command(name="list", description="Players with a bank balance, largest first")
    @app_commands.describe(resource="Sort by this resource (default: total of all five)",
                           count="How many players to show (default 25)")
    async def bank_list(interaction: discord.Interaction, resource: Resource = "total",
                        count: app_commands.Range[int, 1, 100] = 25):
        loaded = await load(interaction)
        if loaded is None:
            return
        accounts, banks = loaded
        await interaction.response.defer()
        ranked = sorted((a for a in accounts if a[resource]), key=lambda a: a[resource], reverse=True)[:count]
        lines = [
            f"{a['name'][:16]:<16} " + " ".join(f"{r[0].upper()}:{compact(a[r]):>7}" for r in RESOURCES)
            for a in ranked
        ]
        await send_list(interaction, f"Bank balances by {resource} (live from {banks} bank{'s' if banks != 1 else ''}):",
                        lines or ["No balances."])

    return bank
