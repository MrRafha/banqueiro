from __future__ import annotations
import asyncio
import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database.db import init_db, recover_active_events, participant_join_voice

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("VelhoCovilBot")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True
intents.voice_states = True
intents.members = True          # Required to fetch/move members
intents.message_content = True  # Required for message content if needed

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = commands.Bot(command_prefix="/", intents=intents)

COGS = [
    "cogs.setup_cog",
    "cogs.events_cog",
    "cogs.finances_cog",
    "cogs.balance_cog",
]


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Connected to {len(bot.guilds)} guild(s)")
    # Global sync (takes up to 1 h to propagate everywhere)
    await bot.tree.sync()
    # Per-guild instant sync so commands appear immediately
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info(f"Tree synced to guild: {guild.name} ({guild.id})")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="o Velho Covil ⚔️",
        )
    )

    # ── Voice-state recovery ─────────────────────────────────────────────────
    # After a restart, participants who were in voice still have join_time set
    # in the DB from *before* the downtime.  Reset it to now so the elapsed
    # time during the outage is not incorrectly counted.
    try:
        rows = await recover_active_events()
        if rows:
            now = time.time()
            for row in rows:
                await participant_join_voice(row["event_id"], int(row["user_id"]), now)
            log.info(
                f"Voice-state recovery: reset join_time for {len(rows)} participant(s) "
                f"across active events."
            )
        else:
            log.info("Voice-state recovery: no active participants to reset.")
    except Exception as exc:
        log.warning(f"Voice-state recovery failed: {exc}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Send a friendly setup message to the first available text channel."""
    target = (
        guild.system_channel
        or next(
            (
                ch for ch in guild.text_channels
                if ch.permissions_for(guild.me).send_messages
            ),
            None,
        )
    )
    if not target:
        return

    embed = discord.Embed(
        title="⚔️  Olá, Velho Covil!",
        description=(
            "Obrigado por me adicionar ao servidor!\n\n"
            "Para começar, um administrador deve usar o comando `/setup` para configurar "
            "os cargos e o canal de log da guilda.\n\n"
            "Se precisar de ajuda, consulte a documentação do bot."
        ),
        colour=discord.Colour.gold(),
    )
    embed.set_footer(text="Velho Covil Bot")
    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        pass


@bot.event
async def on_disconnect():
    log.warning("Bot disconnected from Discord — discord.py will auto-reconnect.")


@bot.event
async def on_resumed():
    log.info("Connection to Discord resumed successfully.")


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global fallback error handler for slash commands."""
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"❌  {error}", colour=discord.Colour.red()
                ),
                ephemeral=True,
            )
        except discord.InteractionResponded:
            pass
    else:
        cmd = interaction.command.name if interaction.command else "?"
        log.exception(f"Unhandled error in command {cmd}: {error}")
        try:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="❌  Ocorreu um erro inesperado. Tente novamente.",
                    colour=discord.Colour.red(),
                ),
                ephemeral=True,
            )
        except discord.InteractionResponded:
            pass


async def main():
    # Initialise database
    await init_db()
    log.info("Database initialised.")

    # Load cogs
    for cog in COGS:
        await bot.load_extension(cog)
        log.info(f"Loaded extension: {cog}")

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN not found. Create a .env file based on .env.example."
        )

    await bot.start(token)


if __name__ == "__main__":
    import sys
    # Windows: switch to SelectorEventLoop to avoid ProactorEventLoop
    # instability with many concurrent aiosqlite/HTTP operations.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
