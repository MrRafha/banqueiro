from __future__ import annotations
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime

from database.db import get_guild_config, create_raid, get_raid_by_message
from utils.embeds import error_embed

# ── Load raid roles config ────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config_raids.json")
with open(_CONFIG_PATH, encoding="utf-8") as _f:
    _RAID_CONFIG: dict = json.load(_f)

_TIPOS_VALIDOS = list(_RAID_CONFIG["roles"].keys())
_TIPO_CHOICES = [app_commands.Choice(name=t.title(), value=t) for t in _TIPOS_VALIDOS]


# ── RaidModal ─────────────────────────────────────────────────────────────

class RaidModal(discord.ui.Modal, title="⚔️  Criar Raid"):
    """Pop-up form for raid details. Shown immediately after /criar_raid."""

    titulo = discord.ui.TextInput(
        label="Título",
        placeholder="ex: Dungeon do Norte — Roaming",
        min_length=1,
        max_length=80,
        style=discord.TextStyle.short,
    )
    descricao = discord.ui.TextInput(
        label="Descrição",
        placeholder="Detalhes da raid...",
        min_length=1,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )
    data = discord.ui.TextInput(
        label="Data",
        placeholder="DD/MM/AAAA  —  ex: 25/12/2025",
        min_length=8,
        max_length=10,
        style=discord.TextStyle.short,
    )
    horario = discord.ui.TextInput(
        label="Horário",
        placeholder="HH:MM  —  ex: 21:00",
        min_length=4,
        max_length=5,
        style=discord.TextStyle.short,
    )
    requisitos = discord.ui.TextInput(
        label="Requisitos (opcional)",
        placeholder="ex: mín. 15 players | cores: MT, MH, Bruxo",
        required=False,
        max_length=150,
        style=discord.TextStyle.short,
    )

    def __init__(self, cog: "RaidsCog", tipo: str):
        super().__init__()
        self.cog = cog
        self.tipo = tipo

    async def on_submit(self, interaction: discord.Interaction):
        self.cog._temp_data[interaction.user.id] = {
            "tipo": self.tipo,
            "titulo": self.titulo.value.strip(),
            "descricao": self.descricao.value.strip(),
            "data": self.data.value.strip(),
            "horario": self.horario.value.strip(),
            "requisitos": self.requisitos.value.strip(),
        }
        view = RoleSelect(cog=self.cog, user_id=interaction.user.id, tipo=self.tipo)
        await interaction.response.send_message(
            f"**{self.titulo.value.strip()}** — selecione as funções:",
            view=view,
            ephemeral=True,
        )

# ── RoleSelect View ───────────────────────────────────────────────────────────

class RoleSelect(discord.ui.View):
    """Ephemeral view shown to the raid creator to pick which roles to include."""

    def __init__(self, cog: "RaidsCog", user_id: int, tipo: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.tipo = tipo
        self.selected_roles: set[str] = set()

        all_groups: dict = _RAID_CONFIG["roles"][tipo]
        selects_added = 0

        for group_name, group_roles in all_groups.items():
            if selects_added >= 4:
                # Discord limits a View to 5 rows; reserve row 5 for the button
                break

            options: list[discord.SelectOption] = []
            for key, role in group_roles.items():
                try:
                    emoji = discord.PartialEmoji.from_str(role["emoji"])
                    options.append(
                        discord.SelectOption(label=role["nome"], value=key, emoji=emoji)
                    )
                except Exception as exc:
                    print(f"[raids] Emoji inválido para '{role['nome']}': {exc}")

            if not options:
                continue

            select = discord.ui.Select(
                placeholder=f"Funções — {group_name}",
                min_values=0,
                max_values=len(options),
                options=options,
                custom_id=f"raid_sel_{group_name}",
            )
            select.callback = self._select_callback
            self.add_item(select)
            selects_added += 1

        confirm_btn = discord.ui.Button(
            label="Confirmar Seleção",
            style=discord.ButtonStyle.success,
            emoji="⚔️",
        )
        confirm_btn.callback = self._confirm_callback
        self.add_item(confirm_btn)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Você não pode interagir com esta seleção.", ephemeral=True
            )

        group_name = interaction.data["custom_id"].removeprefix("raid_sel_")
        group_roles = _RAID_CONFIG["roles"][self.tipo].get(group_name, {})

        # Remove previous selections from this group, then add current ones
        self.selected_roles -= set(group_roles.keys())
        self.selected_roles.update(interaction.data.get("values", []))

        nomes = [
            group_roles[v]["nome"]
            for v in interaction.data.get("values", [])
            if v in group_roles
        ]
        label = ", ".join(nomes) if nomes else "nenhuma"
        await interaction.response.send_message(
            f"**{group_name}** selecionado: {label}", ephemeral=True
        )

    async def _confirm_callback(self, interaction: discord.Interaction):
        # ── Sync validation (no awaits — interaction token still valid) ────────
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Você não pode confirmar a seleção de outro usuário.", ephemeral=True
            )

        if not self.selected_roles:
            return await interaction.response.send_message(
                "Selecione ao menos uma função antes de confirmar.", ephemeral=True
            )

        raid = self.cog._temp_data.pop(interaction.user.id, None)
        if not raid:
            return await interaction.response.send_message(
                embed=error_embed("Dados da raid expirados. Use `/criar_raid` novamente."),
                ephemeral=True,
            )

        # Defer NOW — adding many reactions can take several seconds and would
        # push us past Discord's 3-second interaction response window.
        await interaction.response.defer(ephemeral=True)

        # ── Build embed ───────────────────────────────────────────────────────
        descricao = raid["descricao"].replace("\\n", "\n")

        try:
            dt = datetime.strptime(f"{raid['data']} {raid['horario']}", "%d/%m/%Y %H:%M")
            unix_ts = int(dt.timestamp())
            ts_str = f"<t:{unix_ts}:R>"
        except ValueError:
            ts_str = "(data/horário inválido)"

        desc_lines = [
            descricao,
            "",
            f"📅 **Data:** {raid['data']}     ⏰ **Horário:** {raid['horario']}  {ts_str}",
            f"🗂️ **Tipo:** {raid['tipo'].title()}",
        ]
        if raid.get("requisitos"):
            desc_lines.append(f"📋 **Requisitos:** {raid['requisitos']}")

        embed = discord.Embed(
            title=f"⚔️  {raid['titulo']}",
            description="\n".join(desc_lines),
            colour=discord.Colour.from_rgb(162, 32, 32),
        )
        embed.set_footer(text="Velho Covil Bot • Reaja no emoji da sua função para entrar")

        # Collect roles in group order so the embed layout is predictable
        ordered_roles: list[tuple[str, dict]] = []
        for group_roles in _RAID_CONFIG["roles"][raid["tipo"]].values():
            for role_id, role_data in group_roles.items():
                if role_id in self.selected_roles:
                    ordered_roles.append((role_id, role_data))

        for _role_id, role_data in ordered_roles:
            embed.add_field(
                name=f"{role_data['emoji']} {role_data['nome']} (0)",
                value="(vazio)",
                inline=True,
            )

        msg = await interaction.channel.send(embed=embed)

        # Add reaction for each selected role
        for _role_id, role_data in ordered_roles:
            try:
                await msg.add_reaction(role_data["emoji"])
            except discord.HTTPException as exc:
                print(f"[raids] Não foi possível adicionar reação '{role_data['emoji']}': {exc}")

        # Persist in DB so reactions survive restarts
        await create_raid(
            message_id=msg.id,
            guild_id=interaction.guild_id,
            tipo=raid["tipo"],
            titulo=raid["titulo"],
            descricao=raid["descricao"],
            data=raid["data"],
            horario=raid["horario"],
            selected_roles_json=json.dumps(list(self.selected_roles)),
        )

        # Use edit_original_response since we deferred above
        await interaction.edit_original_response(content="✅ Raid criada com sucesso!", view=None)


# ── RaidsCog ──────────────────────────────────────────────────────────────────

class RaidsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Temporary storage while the creator is picking roles (user_id → raid dict)
        self._temp_data: dict[int, dict] = {}

    # ── /criar_raid ───────────────────────────────────────────────────────────

    @app_commands.command(name="criar_raid", description="Abre o formulário para criar uma raid")
    @app_commands.describe(tipo="Tipo de conteúdo da raid")
    @app_commands.choices(tipo=_TIPO_CHOICES)
    async def criar_raid(
        self,
        interaction: discord.Interaction,
        tipo: app_commands.Choice[str],
    ):
        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado. Use `/setup` primeiro."),
                ephemeral=True,
            )

        admin_id = config["admin_role_id"]
        cc_id = config["cc_role_id"]
        allowed = any(
            interaction.guild.get_role(int(rid)) in interaction.user.roles
            for rid in [admin_id, cc_id]
            if rid
        )
        if not allowed:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Apenas administradores ou criadores de conteúdo podem criar raids."
                ),
                ephemeral=True,
            )

        # send_modal IS the interaction response — no defer needed
        await interaction.response.send_modal(RaidModal(cog=self, tipo=tipo.value))

    # ── Reactions: signup / unsignup ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        raid = await get_raid_by_message(payload.message_id)
        if not raid:
            return  # not a raid message — let events_cog handle it if needed

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            member = payload.member or await guild.fetch_member(payload.user_id)
        except Exception as exc:
            print(f"[raids] on_raw_reaction_add fetch error: {exc}")
            return

        embed = message.embeds[0] if message.embeds else None
        if not embed:
            return

        emoji = str(payload.emoji)
        tipo = raid["tipo"]

        for i, field in enumerate(embed.fields):
            role_data = _find_role_by_emoji(tipo, emoji)
            if not role_data:
                continue
            if role_data["emoji"] not in field.name:
                continue

            nome = member.display_name
            lines = [
                ln.strip()
                for ln in field.value.splitlines()
                if ln.strip() and ln.strip() != "(vazio)"
            ]
            if nome in lines or any(nome in ln for ln in lines):
                return  # already signed up

            lines.append(nome)
            numbered = "\n".join(f"{idx + 1}. {n}" for idx, n in enumerate(lines))
            new_name = f"{role_data['emoji']} {role_data['nome']} ({len(lines)})"
            embed.set_field_at(i, name=new_name, value=numbered, inline=True)
            await message.edit(embed=embed)
            return

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        raid = await get_raid_by_message(payload.message_id)
        if not raid:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            member = await guild.fetch_member(payload.user_id)
        except Exception as exc:
            print(f"[raids] on_raw_reaction_remove fetch error: {exc}")
            return

        embed = message.embeds[0] if message.embeds else None
        if not embed:
            return

        emoji = str(payload.emoji)
        tipo = raid["tipo"]

        for i, field in enumerate(embed.fields):
            role_data = _find_role_by_emoji(tipo, emoji)
            if not role_data:
                continue
            if role_data["emoji"] not in field.name:
                continue

            nome = member.display_name
            lines = [
                ln.strip()
                for ln in field.value.splitlines()
                if ln.strip() and ln.strip() != "(vazio)"
            ]
            # Remove the entry that contains this display name (strip leading "N. ")
            new_lines = [ln for ln in lines if nome not in ln]

            if not new_lines:
                new_value = "(vazio)"
            else:
                # Re-number from 1
                new_value = "\n".join(
                    f"{idx + 1}. {ln.split('. ', 1)[-1]}"
                    for idx, ln in enumerate(new_lines)
                )

            new_name = f"{role_data['emoji']} {role_data['nome']} ({len(new_lines)})"
            embed.set_field_at(i, name=new_name, value=new_value, inline=True)
            await message.edit(embed=embed)
            return


# ── Module-level helper ───────────────────────────────────────────────────────

def _find_role_by_emoji(tipo: str, emoji: str) -> dict | None:
    """Return the role dict that matches *emoji* within the given *tipo*, or None."""
    for group_roles in _RAID_CONFIG["roles"].get(tipo, {}).values():
        for role_data in group_roles.values():
            if role_data["emoji"] == emoji:
                return role_data
    return None


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(RaidsCog(bot))
