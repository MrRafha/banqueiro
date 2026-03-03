from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from database.db import (
    get_guild_config, get_balance, add_balance, set_balance,
    add_guild_balance, subtract_guild_balance, record_transaction,
    atomic_subtract_balance, get_transactions, get_event_history,
    get_all_balances,
)
from utils.checks import is_admin, is_member
from utils.embeds import (
    balance_embed, guild_balance_embed, error_embed, success_embed,
)
from utils.formatters import parse_silver, format_silver, format_pct
from utils.logger import audit_log


LIMIT = 10  # transactions per page


def _extrato_view(user_id: int, page: int, has_prev: bool, has_next: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=120)
    if has_prev:
        view.add_item(discord.ui.Button(
            label="◄ Anterior",
            style=discord.ButtonStyle.secondary,
            custom_id=f"extrato_prev_{user_id}_{page}",
        ))
    if has_next:
        view.add_item(discord.ui.Button(
            label="Próximo ►",
            style=discord.ButtonStyle.secondary,
            custom_id=f"extrato_next_{user_id}_{page}",
        ))
    return view


def _extrato_embed(rows: list, page: int) -> discord.Embed:
    lines: list[str] = []
    for row in rows:
        sign = "+" if row["amount"] > 0 else ""
        note = f" — {row['note']}" if row["note"] else ""
        ts = f"<t:{int(row['created_at'])}:f>" if row["created_at"] else "?"
        lines.append(f"`{sign}{format_silver(row['amount'])}` prata  ({row['type']}{note})  {ts}")
    embed = discord.Embed(
        title=f"📋 Extrato — Página {page}",
        description="\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text="Velho Covil Bot")
    return embed


class BalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Pagination buttons for /extrato ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid: str = interaction.data.get("custom_id", "")
        if not (cid.startswith("extrato_prev_") or cid.startswith("extrato_next_")):
            return

        parts = cid.split("_")
        # format: extrato_{prev|next}_{user_id}_{page}
        if len(parts) != 4:
            return
        direction, user_id_str, page_str = parts[1], parts[2], parts[3]
        try:
            owner_id = int(user_id_str)
            cur_page = int(page_str)
        except ValueError:
            return

        # Only the original requester can paginate
        if interaction.user.id != owner_id:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Apenas quem executou o comando pode navegar.", colour=discord.Colour.red()),
                ephemeral=True,
            )

        new_page = cur_page - 1 if direction == "prev" else cur_page + 1
        new_page = max(1, new_page)

        offset = (new_page - 1) * LIMIT
        # Fetch one extra to detect if there's a next page
        rows = await get_transactions(interaction.guild_id, owner_id, limit=LIMIT + 1, offset=offset)
        has_next = len(rows) > LIMIT
        rows = rows[:LIMIT]

        if not rows:
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"Nenhuma transação na página {new_page}.", colour=discord.Colour.red()),
                ephemeral=True,
            )

        embed = _extrato_embed(rows, new_page)
        view = _extrato_view(owner_id, new_page, has_prev=new_page > 1, has_next=has_next)
        await interaction.response.edit_message(embed=embed, view=view)

    @app_commands.command(
        name="meu-saldo",
        description="Mostra o seu saldo atual de prata.",
    )
    @is_member()
    async def meu_saldo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        balance = await get_balance(interaction.user.id, interaction.guild_id)
        await interaction.followup.send(
            embed=balance_embed(interaction.user, balance), ephemeral=True
        )

    @app_commands.command(
        name="transferir",
        description="Transfere prata do seu saldo para outro membro.",
    )
    @app_commands.describe(membro="Destinatário da transferência", valor="Quantidade de prata (ex: 500000)")
    @is_member()
    async def transferir(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
        valor: str,
    ):
        await interaction.response.defer(ephemeral=True)

        if membro.id == interaction.user.id:
            return await interaction.followup.send(
                embed=error_embed("Você não pode transferir para si mesmo."), ephemeral=True
            )
        if membro.bot:
            return await interaction.followup.send(
                embed=error_embed("Você não pode transferir para um bot."), ephemeral=True
            )

        amount = parse_silver(valor)
        if amount is None or amount <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"Valor inválido: `{valor}`."), ephemeral=True
            )

        # Atomic subtract — prevents TOCTOU race condition
        success = await atomic_subtract_balance(interaction.user.id, interaction.guild_id, amount)
        if not success:
            sender_balance = await get_balance(interaction.user.id, interaction.guild_id)
            return await interaction.followup.send(
                embed=error_embed(
                    f"Saldo insuficiente. Seu saldo: **{format_silver(sender_balance)}** prata."
                ),
                ephemeral=True,
            )

        await add_balance(membro.id, interaction.guild_id, amount)

        await record_transaction(
            guild_id=interaction.guild_id,
            amount=-amount,
            type_="transfer",
            user_id=interaction.user.id,
            note=f"→ {membro.id}",
        )
        await record_transaction(
            guild_id=interaction.guild_id,
            amount=amount,
            type_="transfer",
            user_id=membro.id,
            note=f"← {interaction.user.id}",
        )

        new_balance = await get_balance(interaction.user.id, interaction.guild_id)
        await audit_log(
            self.bot, interaction.guild_id,
            f"💸 {interaction.user.mention} transferiu **{format_silver(amount)}** prata para {membro.mention}.",
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Transferência realizada!\n"
                f"**{format_silver(amount)}** prata enviada para {membro.mention}.\n"
                f"Seu novo saldo: **{format_silver(new_balance)}** prata."
            ),
            ephemeral=True,
        )

        # Notify recipient via DM
        try:
            await membro.send(
                f"💰 Você recebeu **{format_silver(amount)}** prata de {interaction.user.mention} "
                f"no servidor **{interaction.guild.name}**!"
            )
        except discord.Forbidden:
            pass

    @app_commands.command(
        name="pagar",
        description="Retira prata do saldo de um membro (marca como pago). (Admin)",
    )
    @app_commands.describe(membro="Membro a ser pago", valor="Quantidade de prata a retirar do saldo")
    @is_admin()
    async def pagar(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
        valor: str,
    ):
        await interaction.response.defer(ephemeral=True)

        amount = parse_silver(valor)
        if amount is None or amount <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"Valor inválido: `{valor}`."), ephemeral=True
            )

        # Atomic subtract — prevents TOCTOU race condition
        success = await atomic_subtract_balance(membro.id, interaction.guild_id, amount)
        if not success:
            current = await get_balance(membro.id, interaction.guild_id)
            return await interaction.followup.send(
                embed=error_embed(
                    f"Saldo insuficiente de {membro.mention}.\n"
                    f"Saldo atual: **{format_silver(current)}** prata.\n"
                    f"Tentando retirar: **{format_silver(amount)}** prata."
                ),
                ephemeral=True,
            )
        await record_transaction(
            guild_id=interaction.guild_id,
            amount=-amount,
            type_="pay",
            user_id=membro.id,
            note=f"Pago por {interaction.user.id}",
        )

        new_balance = await get_balance(membro.id, interaction.guild_id)
        await audit_log(
            self.bot, interaction.guild_id,
            f"✅ {interaction.user.mention} pagou **{format_silver(amount)}** prata a {membro.mention}.",
        )
        await interaction.followup.send(
            embed=success_embed(
                f"✅ Pagamento registrado!\n"
                f"{membro.mention} — **{format_silver(amount)}** prata retirada.\n"
                f"Saldo restante: **{format_silver(new_balance)}** prata."
            ),
            ephemeral=True,
        )

        try:
            await membro.send(
                f"✅ O administrador {interaction.user.mention} registrou o pagamento de "
                f"**{format_silver(amount)}** prata no servidor **{interaction.guild.name}**.\n"
                f"Seu saldo atual: **{format_silver(new_balance)}** prata."
            )
        except discord.Forbidden:
            pass

    @app_commands.command(
        name="depositar-guild",
        description="Deposita prata manualmente no saldo da guilda. (Admin)",
    )
    @app_commands.describe(valor="Quantidade de prata")
    @is_admin()
    async def depositar_guild(self, interaction: discord.Interaction, valor: str):
        await interaction.response.defer(ephemeral=True)

        amount = parse_silver(valor)
        if amount is None or amount <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"Valor inválido: `{valor}`."), ephemeral=True
            )

        await add_guild_balance(interaction.guild_id, amount)
        await record_transaction(
            guild_id=interaction.guild_id,
            amount=amount,
            type_="guild_deposit",
            user_id=interaction.user.id,
            note="Depósito manual na guilda",
        )

        # Re-fetch after mutation to get accurate new balance
        config = await get_guild_config(interaction.guild_id)
        new_balance = config["guild_balance"] if config else amount
        await audit_log(
            self.bot, interaction.guild_id,
            f"🏦 {interaction.user.mention} depositou **{format_silver(amount)}** prata na guilda.",
        )

        await interaction.followup.send(
            embed=success_embed(
                f"**{format_silver(amount)}** prata depositada no cofre da guilda.\n"
                f"Novo saldo da guilda: **{format_silver(new_balance)}** prata."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="saldo-guilda",
        description="Exibe o saldo atual da guilda. (Admin)",
    )
    @is_admin()
    async def saldo_guilda(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.followup.send(
                embed=error_embed("Bot não configurado. Use `/setup`."), ephemeral=True
            )

        balance = config["guild_balance"] or 0
        tax_rate = config["tax_rate"] or 0
        await interaction.followup.send(
            embed=guild_balance_embed(balance, tax_rate), ephemeral=True
        )

    @app_commands.command(
        name="sacar-guilda",
        description="Saca prata do cofre da guilda. (Admin)",
    )
    @app_commands.describe(valor="Quantidade de prata a sacar", motivo="Motivo do saque (opcional)")
    @is_admin()
    async def sacar_guilda(
        self,
        interaction: discord.Interaction,
        valor: str,
        motivo: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        amount = parse_silver(valor)
        if amount is None or amount <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"Valor inválido: `{valor}`."), ephemeral=True
            )

        success = await subtract_guild_balance(interaction.guild_id, amount)
        if not success:
            config = await get_guild_config(interaction.guild_id)
            current = config["guild_balance"] if config else 0
            return await interaction.followup.send(
                embed=error_embed(
                    f"Saldo insuficiente no cofre da guilda.\n"
                    f"Saldo atual: **{format_silver(current or 0)}** prata.\n"
                    f"Tentando sacar: **{format_silver(amount)}** prata."
                ),
                ephemeral=True,
            )

        await record_transaction(
            guild_id=interaction.guild_id,
            amount=-amount,
            type_="guild_withdraw",
            user_id=interaction.user.id,
            note=motivo or "Saque da guilda",
        )

        config = await get_guild_config(interaction.guild_id)
        new_balance = config["guild_balance"] if config else 0
        await audit_log(
            self.bot, interaction.guild_id,
            f"💸 {interaction.user.mention} sacou **{format_silver(amount)}** prata da guilda."
            + (f" Motivo: {motivo}" if motivo else ""),
        )

        await interaction.followup.send(
            embed=success_embed(
                f"💸 Saque realizado!\n"
                f"**{format_silver(amount)}** prata sacada do cofre da guilda.\n"
                f"Saldo restante: **{format_silver(new_balance or 0)}** prata."
                + (f"\nMotivo: {motivo}" if motivo else "")
            ),
            ephemeral=True,
        )

    # ── /extrato ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="extrato",
        description="Exibe seu histórico de transações recentes.",
    )
    @app_commands.describe(pagina="Número da página (padrão: 1)")
    @is_member()
    async def extrato(self, interaction: discord.Interaction, pagina: int = 1):
        await interaction.response.defer(ephemeral=True)

        page = max(pagina, 1)
        offset = (page - 1) * LIMIT
        # Fetch one extra to know if there is a next page
        rows = await get_transactions(interaction.guild_id, interaction.user.id, limit=LIMIT + 1, offset=offset)
        has_next = len(rows) > LIMIT
        rows = rows[:LIMIT]

        if not rows:
            msg = "Nenhuma transação encontrada." if page == 1 else f"Nenhuma transação na página {page}."
            return await interaction.followup.send(embed=error_embed(msg), ephemeral=True)

        embed = _extrato_embed(rows, page)
        view = _extrato_view(interaction.user.id, page, has_prev=page > 1, has_next=has_next)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /historico ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="historico",
        description="Exibe seu histórico de participações em eventos.",
    )
    @is_member()
    async def historico(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rows = await get_event_history(interaction.guild_id, interaction.user.id)
        if not rows:
            return await interaction.followup.send(
                embed=error_embed("Nenhum evento encontrado no histórico."), ephemeral=True
            )

        embed = discord.Embed(
            title="⚔️  Histórico de Eventos",
            colour=discord.Colour.gold(),
        )
        for row in rows[:15]:  # cap at 15 to avoid embed size limit
            pct = format_pct(row["participation_pct"] or 0)
            silver_total = row["silver_amount"] or 0
            earned = silver_total * (row["participation_pct"] or 0) / 100
            ts_val = row["ended_at"] or row["started_at"]
            ts = f"<t:{int(ts_val)}:d>" if ts_val else "?"
            embed.add_field(
                name=f"Evento `{str(row['id'])[:8]}`  —  {ts}",
                value=f"Participação: `{pct}`  |  Prata recebida: **{format_silver(earned)}**",
                inline=False,
            )
        embed.set_footer(text="Velho Covil Bot")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="saldos",
        description="Lista o saldo de todos os membros, do maior para o menor. (Admin)",
    )
    @is_admin()
    async def saldos(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rows = await get_all_balances(interaction.guild_id)
        if not rows:
            return await interaction.followup.send(
                embed=error_embed("Nenhum membro possui saldo no momento."), ephemeral=True
            )

        medals = ["🥇", "🥈", "🥉"]
        lines: list[str] = []
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} <@{row['user_id']}> — **{format_silver(row['balance'])}** prata")

        # Discord embed description limit is 4096 chars; chunk if needed
        chunks: list[list[str]] = [[]]
        for line in lines:
            if sum(len(l) for l in chunks[-1]) + len(line) > 3800:
                chunks.append([])
            chunks[-1].append(line)

        total = sum(row["balance"] for row in rows)
        first = True
        for chunk in chunks:
            embed = discord.Embed(
                title="🏆 Ranking de Saldos" if first else None,
                description="\n".join(chunk),
                colour=discord.Colour.gold(),
            )
            if first:
                embed.add_field(
                    name="Total em circulação",
                    value=f"**{format_silver(total)}** prata entre {len(rows)} membro(s)",
                    inline=False,
                )
                embed.set_footer(text="Velho Covil Bot")
                first = False
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(embed=error_embed(str(error)), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(BalanceCog(bot))
