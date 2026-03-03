from __future__ import annotations
import time
import discord
from discord.ext import commands
from database.db import get_guild_config


async def audit_log(bot: commands.Bot, guild_id: int, message: str,
                    embed: discord.Embed | None = None):
    """Send an audit message to the guild's log channel if configured."""
    try:
        config = await get_guild_config(guild_id)
        if not config or not config["log_channel_id"]:
            return
        channel = bot.get_channel(int(config["log_channel_id"]))
        if not channel:
            return

        if embed is None:
            embed = discord.Embed(
                description=message,
                colour=discord.Colour.from_rgb(100, 100, 100),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="Auditoria | Velho Covil Bot")

        await channel.send(embed=embed)
    except Exception:
        pass  # Never let logging break the main flow
