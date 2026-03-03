from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from database.db import get_guild_config


def _error(msg: str):
    async def predicate(interaction: discord.Interaction):
        raise app_commands.CheckFailure(msg)
    return predicate


async def _get_config(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild_id)
    if not config:
        raise app_commands.CheckFailure(
            "O bot ainda não foi configurado. Use `/setup` primeiro."
        )
    return config


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await _get_config(interaction)
        role_id = config["admin_role_id"]
        if not role_id:
            raise app_commands.CheckFailure("Role de administrador não configurada.")
        role = interaction.guild.get_role(int(role_id))
        if role and role in interaction.user.roles:
            return True
        raise app_commands.CheckFailure("Você não tem permissão para usar este comando.")
    return app_commands.check(predicate)


def is_seller():
    """Admin OR seller can use."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await _get_config(interaction)
        admin_id = config["admin_role_id"]
        seller_id = config["seller_role_id"]
        user_roles = interaction.user.roles
        roles_ok = []
        if admin_id:
            roles_ok.append(interaction.guild.get_role(int(admin_id)))
        if seller_id:
            roles_ok.append(interaction.guild.get_role(int(seller_id)))
        for r in roles_ok:
            if r and r in user_roles:
                return True
        raise app_commands.CheckFailure(
            "Apenas administradores ou vendedores podem usar este comando."
        )
    return app_commands.check(predicate)


def is_cc():
    """Admin OR content creator can use."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await _get_config(interaction)
        admin_id = config["admin_role_id"]
        cc_id = config["cc_role_id"]
        user_roles = interaction.user.roles
        roles_ok = []
        if admin_id:
            roles_ok.append(interaction.guild.get_role(int(admin_id)))
        if cc_id:
            roles_ok.append(interaction.guild.get_role(int(cc_id)))
        for r in roles_ok:
            if r and r in user_roles:
                return True
        raise app_commands.CheckFailure(
            "Apenas administradores ou criadores de conteúdo podem usar este comando."
        )
    return app_commands.check(predicate)


def is_member():
    """Admin, CC, seller, or member can use."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await _get_config(interaction)
        keys = ["admin_role_id", "cc_role_id", "seller_role_id", "member_role_id"]
        user_roles = interaction.user.roles
        for k in keys:
            rid = config[k]
            if rid:
                role = interaction.guild.get_role(int(rid))
                if role and role in user_roles:
                    return True
        raise app_commands.CheckFailure(
            "Você precisa ser membro da guilda para usar este comando."
        )
    return app_commands.check(predicate)
