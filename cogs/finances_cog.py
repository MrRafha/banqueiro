from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from database.db import (
    get_guild_config, get_event_by_channel, get_event,
    get_participants, update_event, upsert_participant,
    remove_participant, add_balance, add_guild_balance, record_transaction,
    batch_credit_event,
)
from utils.logger import audit_log
from utils.checks import is_admin, is_seller
from utils.embeds import (
    event_summary_embed, simulation_embed, confirmation_embed,
    error_embed, success_embed,
)
from utils.formatters import parse_silver, format_silver


def _compute_splits(participants: list, silver: float, tax_rate: float) -> tuple[list[dict], bool]:
    """Return (splits, equal_fallback). If total_pct==0, distributes equally."""
    guild_cut = silver * (tax_rate / 100)
    distributable = silver - guild_cut
    total_pct = sum(p["participation_pct"] or 0 for p in participants)
    splits = []
    equal_fallback = total_pct == 0
    n = len(participants)
    for p in participants:
        pct = p["participation_pct"] or 0
        if equal_fallback:
            amount = distributable / n if n > 0 else 0
            pct = 100.0 / n if n > 0 else 0
        else:
            amount = distributable * (pct / total_pct)
        splits.append({"user_id": p["user_id"], "pct": pct, "amount": amount})
    return splits, equal_fallback


def _sim_view(event_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(
        discord.ui.Button(
            label="✅  Confirmar Split",
            style=discord.ButtonStyle.success,
            custom_id=f"confirm_split_{event_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="❌  Cancelar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"cancel_split_{event_id}",
        )
    )
    return view


class FinancesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Button handler ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid: str = interaction.data.get("custom_id", "")

        if cid.startswith("confirm_split_"):
            event_id = cid[len("confirm_split_"):]
            await self._handle_confirm_split(interaction, event_id)
        elif cid.startswith("cancel_split_"):
            event_id = cid[len("cancel_split_"):]
            await interaction.response.send_message(
                embed=success_embed("Simulação cancelada. Use `/simular` para tentar novamente."),
                ephemeral=True,
            )
            # Disable the buttons
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

    async def _handle_confirm_split(
        self, interaction: discord.Interaction, event_id: str
    ):
        # Admin check
        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado."), ephemeral=True
            )
        admin_id = config["admin_role_id"]
        if admin_id:
            role = interaction.guild.get_role(int(admin_id))
            if not (role and role in interaction.user.roles):
                return await interaction.response.send_message(
                    embed=error_embed("Apenas administradores podem confirmar o split."),
                    ephemeral=True,
                )

        event = await get_event(event_id)
        if not event:
            return await interaction.response.send_message(
                embed=error_embed("Evento não encontrado."), ephemeral=True
            )
        if event["status"] != "ended":
            return await interaction.response.send_message(
                embed=error_embed("Este evento já foi finalizado ou não pode ser confirmado agora."),
                ephemeral=True,
            )

        silver = event["silver_amount"]
        if not silver:
            return await interaction.response.send_message(
                embed=error_embed("Defina a prata com `/setar-prata` antes de confirmar."),
                ephemeral=True,
            )

        await interaction.response.defer()

        # ── Mark as finalized FIRST to prevent double-click race ──────────────
        await update_event(event_id, status="finalized")

        tax_rate = config["tax_rate"] or 0
        guild_cut = silver * (tax_rate / 100)
        participants = await get_participants(event_id)
        splits, equal_fallback = _compute_splits(participants, silver, tax_rate)

        # Credit all participants + guild in a single DB transaction
        short_id = event_id[:8]
        await batch_credit_event(
            event_id=event_id,
            guild_id=interaction.guild_id,
            splits=splits,
            guild_cut=guild_cut,
            short_id=short_id,
        )

        await audit_log(
            self.bot, interaction.guild_id,
            f"💰 Split do evento `{event_id[:8]}` confirmado por {interaction.user.mention}. "
            f"Total: **{silver}** prata | {len(splits)} participantes."
        )

        # Disable simulation buttons
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        extra = "\n⚠️ Distribuição igual (sem % definida)." if equal_fallback else ""
        embed = confirmation_embed(event_id, splits, guild_cut)
        if extra:
            embed.set_footer(text=extra.strip())
        await interaction.followup.send(embed=embed)

    # ── Commands ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="setar-prata",
        description="Define a prata obtida no evento (use no canal do evento). Vend./Admin.",
    )
    @app_commands.describe(valor="Quantidade de prata (ex: 15000000 ou 15.000.000)")
    @is_seller()
    async def setar_prata(self, interaction: discord.Interaction, valor: str):
        await interaction.response.defer(ephemeral=True)

        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] != "ended":
            return await interaction.followup.send(
                embed=error_embed(
                    "Este comando só pode ser usado em um canal de evento encerrado."
                ),
                ephemeral=True,
            )

        amount = parse_silver(valor)
        if amount is None or amount <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"Valor inválido: `{valor}`. Ex: `15000000` ou `15.000.000`"),
                ephemeral=True,
            )

        await update_event(event["id"], silver_amount=amount)

        config = await get_guild_config(interaction.guild_id)
        tax_rate = (config["tax_rate"] or 0) if config else 0
        participants = await get_participants(event["id"])
        duration = (event["ended_at"] or 0) - (event["started_at"] or 0)
        embed = event_summary_embed(event["id"], participants, duration, amount, tax_rate)

        await interaction.channel.send(
            content=f"💰 Prata setada: **{format_silver(amount)}** por {interaction.user.mention}",
            embed=embed,
        )
        await interaction.followup.send(
            embed=success_embed(f"Prata do evento definida: **{format_silver(amount)}**"),
            ephemeral=True,
        )

    @app_commands.command(
        name="add-participante",
        description="Adiciona ou ajusta a % de participação de alguém no evento. (Admin)",
    )
    @app_commands.describe(membro="Membro a adicionar", participacao="% de participação (0-100)")
    @is_admin()
    async def add_participante(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
        participacao: float,
    ):
        await interaction.response.defer(ephemeral=True)

        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] not in ("ended", "active"):
            return await interaction.followup.send(
                embed=error_embed("Este comando só pode ser usado em um canal de evento."),
                ephemeral=True,
            )

        if not (0 <= participacao <= 100):
            return await interaction.followup.send(
                embed=error_embed("A participação deve estar entre 0 e 100%."),
                ephemeral=True,
            )

        await upsert_participant(event["id"], membro.id, participacao, manual_pct=True)

        config = await get_guild_config(interaction.guild_id)
        tax_rate = (config["tax_rate"] or 0) if config else 0
        participants = await get_participants(event["id"])
        duration = (event["ended_at"] or 0) - (event["started_at"] or 0)
        silver = event["silver_amount"]
        embed = event_summary_embed(event["id"], participants, duration, silver, tax_rate)
        await interaction.channel.send(
            content=f"✏️ {interaction.user.mention} ajustou participação de {membro.mention} para **{participacao}%**",
            embed=embed,
        )
        await interaction.followup.send(
            embed=success_embed(f"Participação de {membro.mention} ajustada para **{participacao}%**."),
            ephemeral=True,
        )

    @app_commands.command(
        name="remover-participante",
        description="Remove um participante do evento. (Admin)",
    )
    @app_commands.describe(membro="Membro a remover")
    @is_admin()
    async def remover_participante(
        self, interaction: discord.Interaction, membro: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)

        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] not in ("ended", "active"):
            return await interaction.followup.send(
                embed=error_embed("Este comando só pode ser usado em um canal de evento."),
                ephemeral=True,
            )

        await remove_participant(event["id"], membro.id)

        config = await get_guild_config(interaction.guild_id)
        tax_rate = (config["tax_rate"] or 0) if config else 0
        participants = await get_participants(event["id"])
        duration = (event["ended_at"] or 0) - (event["started_at"] or 0)
        silver = event["silver_amount"]
        embed = event_summary_embed(event["id"], participants, duration, silver, tax_rate)
        await interaction.channel.send(
            content=f"🗑️ {interaction.user.mention} removeu {membro.mention} do evento.",
            embed=embed,
        )
        await interaction.followup.send(
            embed=success_embed(f"{membro.mention} removido do evento."), ephemeral=True
        )

    @app_commands.command(
        name="simular",
        description="Simula o split da prata no evento atual. (Admin)",
    )
    @is_admin()
    async def simular(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] != "ended":
            return await interaction.followup.send(
                embed=error_embed(
                    "Este comando só pode ser usado em um canal de evento encerrado."
                )
            )

        silver = event["silver_amount"]
        if not silver:
            return await interaction.followup.send(
                embed=error_embed(
                    "Defina a prata primeiro com `/setar-prata`."
                )
            )

        config = await get_guild_config(interaction.guild_id)
        tax_rate = (config["tax_rate"] or 0) if config else 0
        participants = await get_participants(event["id"])

        if not participants:
            return await interaction.followup.send(
                embed=error_embed("Nenhum participante registrado no evento.")
            )

        splits, equal_fallback = _compute_splits(participants, silver, tax_rate)
        embed = simulation_embed(event["id"], silver, tax_rate, splits)
        if equal_fallback:
            embed.add_field(
                name="⚠️ Aviso",
                value="Nenhum participante tem % definida. A distribuição será **igual** entre todos.",
                inline=False,
            )
        view = _sim_view(event["id"])

        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(
        name="depositar",
        description="Confirma o split manualmente (normalmente feito pelo botão Confirmar). (Admin)",
    )
    @is_admin()
    async def depositar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] != "ended":
            return await interaction.followup.send(
                embed=error_embed("Use este comando em um canal de evento encerrado."),
                ephemeral=True,
            )

        if not event["silver_amount"]:
            return await interaction.followup.send(
                embed=error_embed("Defina a prata com `/setar-prata` primeiro."),
                ephemeral=True,
            )

        config = await get_guild_config(interaction.guild_id)
        tax_rate = (config["tax_rate"] or 0) if config else 0
        guild_cut = event["silver_amount"] * (tax_rate / 100)
        participants = await get_participants(event["id"])
        splits = _compute_splits(participants, event["silver_amount"], tax_rate)

        # Mark finalized first to prevent race if /depositar and button Confirmar used together
        await update_event(event["id"], status="finalized")

        splits, _ = _compute_splits(participants, event["silver_amount"], tax_rate)
        await batch_credit_event(
            event_id=event["id"],
            guild_id=interaction.guild_id,
            splits=splits,
            guild_cut=guild_cut,
            short_id=event["id"][:8],
        )

        await audit_log(
            self.bot, interaction.guild_id,
            f"💰 Split manual do evento `{event['id'][:8]}` executado por {interaction.user.mention}."
        )

        embed = confirmation_embed(event["id"], splits, guild_cut)
        await interaction.channel.send(embed=embed)
        await interaction.followup.send(embed=success_embed("Split realizado com sucesso!"), ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(embed=error_embed(str(error)), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(FinancesCog(bot))
