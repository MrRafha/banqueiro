from __future__ import annotations
import discord
from utils.formatters import format_silver, format_pct

COLOUR_BRAND  = discord.Colour.from_rgb(162, 32, 32)   # deep red
COLOUR_OK     = discord.Colour.green()
COLOUR_WARN   = discord.Colour.gold()
COLOUR_INFO   = discord.Colour.blurple()
COLOUR_DANGER = discord.Colour.red()


# ─── Setup ────────────────────────────────────────────────────────────────────

def setup_channel_embed(tax_rate: float) -> discord.Embed:
    embed = discord.Embed(
        title="⚔️  Velho Covil — Central de Eventos",
        description=(
            "Bem-vindo ao sistema de eventos da guilda!\n\n"
            "**Como funciona:**\n"
            "1. Clique em **Criar Evento** para abrir uma sala de voz para o evento.\n"
            "2. Reaja com 💀 no anúncio do evento para confirmar participação.\n"
            "3. O bot move automaticamente quem reagiu para a call.\n"
            "4. Ao final, o organizador encerra o evento e o bot calcula as participações.\n"
            "5. O vendedor define a prata obtida e os administradores confirmam o split.\n\n"
            f"**Taxa da guilda atual:** `{format_pct(tax_rate)}`"
        ),
        colour=COLOUR_BRAND,
    )
    embed.set_footer(text="Velho Covil Bot")
    return embed


# ─── Event Announce ───────────────────────────────────────────────────────────

def event_announce_embed(event_id: str, creator: discord.Member,
                          voice_channel: discord.VoiceChannel,
                          event_name: str = "",
                          participant_ids: list[int] | None = None) -> discord.Embed:
    display = event_name or f"Evento `{event_id}`"
    embed = discord.Embed(
        title=f"⚔️  {display}",
        description=(
            f"Um novo evento foi criado por {creator.mention}!\n\n"
            f"**Reaja com 💀 para participar** e ser movido automaticamente para a call.\n\n"
            f"🔊 Canal: {voice_channel.mention}"
        ),
        colour=COLOUR_BRAND,
    )
    embed.add_field(name="ID do Evento", value=f"`{event_id}`", inline=True)

    # Live participant list (names only, no percentages)
    ids = participant_ids or []
    if ids:
        mentions = " ".join(f"<@{uid}>" for uid in ids[:25])
        if len(ids) > 25:
            mentions += f"\n… e mais {len(ids) - 25}"
    else:
        mentions = "—"
    embed.add_field(
        name=f"👥 Participantes ({len(ids)})",
        value=mentions,
        inline=False,
    )
    embed.set_footer(text="Velho Covil Bot • Aguardando participantes…")
    return embed


# ─── Event Summary (text channel) ─────────────────────────────────────────────

def event_summary_embed(event_id: str, participants: list,
                         event_duration: float, silver: float | None = None,
                         tax_rate: float = 0.0, event_name: str = "") -> discord.Embed:
    duration_min = int(event_duration // 60)
    duration_sec = int(event_duration % 60)

    title_suffix = f" — {event_name}" if event_name else ""
    embed = discord.Embed(
        title=f"📋  Resumo do Evento{title_suffix}",
        colour=COLOUR_WARN,
    )
    embed.add_field(name="ID", value=f"`{event_id}`", inline=True)
    embed.add_field(
        name="Duração",
        value=f"`{duration_min}m {duration_sec}s`",
        inline=True,
    )

    if silver is not None:
        guild_cut = silver * (tax_rate / 100)
        distributable = silver - guild_cut
        embed.add_field(name="Prata Total", value=format_silver(silver), inline=True)
        embed.add_field(
            name=f"Taxa da Guilda ({format_pct(tax_rate)})",
            value=format_silver(guild_cut),
            inline=True,
        )
        embed.add_field(
            name="Prata a Distribuir", value=format_silver(distributable), inline=True
        )

    if participants:
        lines = []
        for p in participants:
            uid = p["user_id"]
            pct = p["participation_pct"] or 0
            secs = int(p["total_seconds"] or 0)
            manual = " ✏️" if p["manual_pct"] else ""
            time_str = f"{secs // 60}m {secs % 60}s"
            lines.append(f"<@{uid}> — {format_pct(pct)}{manual} ({time_str})")
        embed.add_field(
            name=f"Participantes ({len(participants)})",
            value="\n".join(lines) or "Nenhum",
            inline=False,
        )

    embed.set_footer(
        text="✏️ = participação ajustada manualmente  |  Velho Covil Bot"
    )
    return embed


# ─── Simulation ───────────────────────────────────────────────────────────────

def simulation_embed(event_id: str, silver: float, tax_rate: float,
                     splits: list[dict]) -> discord.Embed:
    """splits: list of {user_id, pct, amount}"""
    guild_cut = silver * (tax_rate / 100)
    distributable = silver - guild_cut

    embed = discord.Embed(
        title="🔢  Simulação de Split",
        description=(
            f"**Prata total:** {format_silver(silver)}\n"
            f"**Taxa da guilda ({format_pct(tax_rate)}):** {format_silver(guild_cut)}\n"
            f"**A depositar na guilda:** {format_silver(guild_cut)}\n"
            f"**Valor a distribuir:** {format_silver(distributable)}"
        ),
        colour=COLOUR_INFO,
    )

    if splits:
        lines = [
            f"<@{s['user_id']}> — {format_pct(s['pct'])} → **{format_silver(s['amount'])}** prata"
            for s in splits
        ]
        embed.add_field(
            name="Distribuição",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"ID do Evento: {event_id}  |  Velho Covil Bot")
    return embed


# ─── Confirmation ─────────────────────────────────────────────────────────────

def confirmation_embed(event_id: str, splits: list[dict],
                        guild_cut: float) -> discord.Embed:
    embed = discord.Embed(
        title="✅  Split Confirmado!",
        colour=COLOUR_OK,
    )
    lines = [
        f"<@{s['user_id']}> +**{format_silver(s['amount'])}** prata"
        for s in splits
    ]
    embed.add_field(name="Saldos Creditados", value="\n".join(lines) or "—", inline=False)
    embed.add_field(
        name="Guilda", value=f"+{format_silver(guild_cut)} prata creditados à guilda", inline=False
    )
    embed.set_footer(text=f"ID do Evento: {event_id}  |  Velho Covil Bot")
    return embed


# ─── Balance ──────────────────────────────────────────────────────────────────

def balance_embed(member: discord.Member, balance: float) -> discord.Embed:
    embed = discord.Embed(
        title="💰  Meu Saldo",
        description=f"{member.mention}, seu saldo atual é:\n\n**{format_silver(balance)} prata**",
        colour=COLOUR_INFO,
    )
    embed.set_footer(text="Velho Covil Bot")
    return embed


def guild_balance_embed(balance: float, tax_rate: float) -> discord.Embed:
    embed = discord.Embed(
        title="🏰  Saldo da Guilda",
        description=f"**{format_silver(balance)} prata**",
        colour=COLOUR_BRAND,
    )
    embed.add_field(name="Taxa atual", value=format_pct(tax_rate), inline=True)
    embed.set_footer(text="Velho Covil Bot")
    return embed


# ─── Error ────────────────────────────────────────────────────────────────────

def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"❌  {message}", colour=COLOUR_DANGER)


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"✅  {message}", colour=COLOUR_OK)
