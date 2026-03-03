from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from database.db import get_guild_config, upsert_guild_config
from utils.checks import is_admin
from utils.embeds import setup_channel_embed, error_embed, success_embed
from utils.logger import audit_log


def _setup_channel_view() -> discord.ui.View:
    """Buttons for the #criar-evento channel. No persistent View class needed —
    all click handling is done via on_interaction in the respective cogs."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Criar Evento",
            style=discord.ButtonStyle.danger,
            emoji="⚔️",
            custom_id="btn_create_event",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Configurações",
            style=discord.ButtonStyle.secondary,
            emoji="⚙️",
            custom_id="btn_settings",
        )
    )
    return view


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Button: Configurações ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.data.get("custom_id") != "btn_settings":
            return

        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado ainda. Use `/setup`."),
                ephemeral=True,
            )

        admin_role_id = config["admin_role_id"]
        if admin_role_id:
            role = interaction.guild.get_role(int(admin_role_id))
            if not (role and role in interaction.user.roles):
                return await interaction.response.send_message(
                    embed=error_embed("Apenas administradores podem acessar as configurações."),
                    ephemeral=True,
                )

        tax = config["tax_rate"] or 0
        embed = discord.Embed(title="⚙️  Configurações Atuais", colour=discord.Colour.blurple())
        fields = {
            "Role Admin":  config["admin_role_id"],
            "Role CC":     config["cc_role_id"],
            "Role Seller": config["seller_role_id"],
            "Role Membro": config["member_role_id"],
        }
        for name, rid in fields.items():
            embed.add_field(name=name, value=f"<@&{rid}>" if rid else "Não configurada", inline=True)
        embed.add_field(name="Taxa da guilda", value=f"{tax}%", inline=True)
        embed.set_footer(text="Use /setup para reconfigurar  |  Velho Covil Bot")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Commands ──────────────────────────────────────────────────────────────

    @app_commands.command(name="setup", description="Configura o bot para este servidor.")
    @app_commands.describe(admin="Role de administrador", creator="Role de criador de conteúdo", seller="Role de vendedor", member="Role de membro", log_channel="Canal de log/auditoria (opcional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        admin: discord.Role,
        creator: discord.Role,
        seller: discord.Role,
        member: discord.Role,
        log_channel: discord.TextChannel = None,
    ):
        await interaction.response.defer(ephemeral=True)

        config = await get_guild_config(interaction.guild_id)

        # Reuse or create category
        category = None
        if config and config["event_category_id"]:
            category = interaction.guild.get_channel(int(config["event_category_id"]))
        if not category:
            category = await interaction.guild.create_category("Eventos")

        # Channel permissions
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                add_reactions=True,
            ),
            admin: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                add_reactions=True,
            ),
        }

        # Reuse or create #criar-evento
        create_channel = None
        if config and config["create_event_channel_id"]:
            create_channel = interaction.guild.get_channel(int(config["create_event_channel_id"]))
        if not create_channel:
            create_channel = await interaction.guild.create_text_channel(
                "criar-evento",
                category=category,
                overwrites=overwrites,
                topic="Central de criação e acompanhamento de eventos da guilda.",
            )
        else:
            await create_channel.edit(overwrites=overwrites)

        # Save config
        config_kwargs = dict(
            admin_role_id=str(admin.id),
            cc_role_id=str(creator.id),
            seller_role_id=str(seller.id),
            member_role_id=str(member.id),
            event_category_id=str(category.id),
            create_event_channel_id=str(create_channel.id),
        )
        if log_channel:
            config_kwargs["log_channel_id"] = str(log_channel.id)
        await upsert_guild_config(interaction.guild_id, **config_kwargs)

        updated_config = await get_guild_config(interaction.guild_id)
        tax = updated_config["tax_rate"] if updated_config else 0

        # Refresh setup embed in #criar-evento
        await create_channel.purge(limit=10, check=lambda m: m.author == interaction.guild.me)
        await create_channel.send(embed=setup_channel_embed(tax), view=_setup_channel_view())

        log_info = f"\n• Log: {log_channel.mention}" if log_channel else ""
        await interaction.followup.send(
            embed=success_embed(
                f"Configuração concluída!\n"
                f"• Categoria: {category.mention}\n"
                f"• Canal: {create_channel.mention}\n"
                f"• Admin: {admin.mention}  |  CC: {creator.mention}\n"
                f"• Vendedor: {seller.mention}  |  Membro: {member.mention}"
                + log_info
            ),
            ephemeral=True,
        )
        await audit_log(
            self.bot, interaction.guild_id,
            f"⚙️ Bot configurado por {interaction.user.mention}."
        )

    @app_commands.command(
        name="setar-taxa",
        description="Define a taxa que a guilda cobra nos eventos. (Admin)",
    )
    @app_commands.describe(taxa="Taxa em % (ex: 10 para 10%)")
    @is_admin()
    async def setar_taxa(self, interaction: discord.Interaction, taxa: float):
        await interaction.response.defer(ephemeral=True)

        if not (0 <= taxa <= 100):
            return await interaction.followup.send(
                embed=error_embed("A taxa deve estar entre 0 e 100%."), ephemeral=True
            )

        await upsert_guild_config(interaction.guild_id, tax_rate=taxa)

        # Update setup embed live
        config = await get_guild_config(interaction.guild_id)
        if config and config["create_event_channel_id"]:
            channel = interaction.guild.get_channel(int(config["create_event_channel_id"]))
            if channel:
                async for msg in channel.history(limit=10):
                    if msg.author == interaction.guild.me and msg.embeds:
                        await msg.edit(
                            embed=setup_channel_embed(taxa),
                            view=_setup_channel_view(),
                        )
                        break

        await interaction.followup.send(
            embed=success_embed(f"Taxa da guilda atualizada para **{taxa}%**."),
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message(
                embed=error_embed("Você precisa da permissão **Gerenciar Servidor** para usar `/setup`."),
                ephemeral=True,
            )
        elif isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(embed=error_embed(str(error)), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
