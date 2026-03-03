from __future__ import annotations
import asyncio
import time
import uuid
import re
import discord
from discord import app_commands
from discord.ext import commands

from database.db import (
    get_guild_config, create_event, get_event, get_event_by_channel,
    update_event, add_participant, participant_join_voice,
    participant_leave_voice, get_participants, finalize_participation_pcts,
    get_all_active_events, get_active_events,
)
from utils.checks import is_admin
from utils.embeds import (
    event_announce_embed, event_summary_embed, error_embed, success_embed,
)
from utils.logger import audit_log
from utils.formatters import format_pct


def _slug(name: str) -> str:
    """Convert an event name to a valid Discord channel name slug."""
    name = name.strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w-]", "", name, flags=re.UNICODE)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:80] or "evento"


class EventNameModal(discord.ui.Modal):
    """Modal that prompts the event creator to type a name."""

    def __init__(self, cog: "EventsCog", config):
        super().__init__(title="Criar Evento")
        self.cog = cog
        self.config = config
        self.add_item(
            discord.ui.TextInput(
                label="Nome do Evento",
                placeholder="ex: Dungeon do Norte",
                min_length=1,
                max_length=50,
                style=discord.TextStyle.short,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        event_name = self.children[0].value.strip() or "evento"
        await self.cog._do_create_event(interaction, event_name, self.config)


def _start_event_view(event_id: str) -> discord.ui.View:
    """Shows the 'Iniciar Evento' green button before the event is started."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Iniciar Evento",
            style=discord.ButtonStyle.success,
            emoji="⚔️",
            custom_id=f"start_event_{event_id}",
        )
    )
    return view


def _end_event_view(event_id: str) -> discord.ui.View:
    """Creates a View with a single 'Encerrar Evento' button for a given event."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Encerrar Evento",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=f"end_event_{event_id}",
        )
    )
    return view


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-event pending embed-refresh tasks (debounce burst reactions)
        self._embed_refresh_tasks: dict[str, asyncio.Task] = {}

    # ── Debounced announce-embed refresh ─────────────────────────────────────

    def _schedule_embed_refresh(self, event, guild: discord.Guild, config):
        """Cancel any pending refresh for this event and schedule a fresh one
        after a 2-second quiet window.  Multiple rapid reactions collapse into
        a single fetch_message + edit call, keeping HTTP traffic low."""
        event_id = event["id"]
        old = self._embed_refresh_tasks.pop(event_id, None)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(
            self._do_refresh_embed(event_id, guild, config)
        )
        self._embed_refresh_tasks[event_id] = task

    async def _do_refresh_embed(self, event_id: str, guild: discord.Guild, config):
        """Sleep 2 s (debounce), then rebuild the announce embed with the
        current participant list."""
        try:
            await asyncio.sleep(2.0)
            event = await get_event(event_id)
            if not event or event["status"] not in ("pending", "active"):
                return
            ch_id = config["create_event_channel_id"]
            if not ch_id:
                return
            announce_ch = guild.get_channel(int(ch_id))
            if not announce_ch:
                return
            ann_msg = await announce_ch.fetch_message(int(event["announce_msg_id"]))
            all_parts = await get_participants(event_id)
            ids = [int(p["user_id"]) for p in all_parts]
            if ann_msg.embeds:
                old_e = ann_msg.embeds[0]
                new_e = discord.Embed.from_dict(old_e.to_dict())
                new_e.clear_fields()
                for f in old_e.fields:
                    if not f.name.startswith("👥 Participantes"):
                        new_e.add_field(name=f.name, value=f.value, inline=f.inline)
                if ids:
                    mentions = " ".join(f"<@{uid}>" for uid in ids[:25])
                    if len(ids) > 25:
                        mentions += f"\n… e mais {len(ids) - 25}"
                else:
                    mentions = "—"
                new_e.add_field(
                    name=f"👥 Participantes ({len(ids)})",
                    value=mentions,
                    inline=False,
                )
                cur_view = (
                    _start_event_view(event_id)
                    if event["status"] == "pending"
                    else _end_event_view(event_id)
                )
                await ann_msg.edit(embed=new_e, view=cur_view)
        except asyncio.CancelledError:
            pass  # A newer refresh was scheduled; this one is obsolete
        except Exception:
            pass
        finally:
            self._embed_refresh_tasks.pop(event_id, None)

    # ── Intercept button interactions ─────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid: str = interaction.data.get("custom_id", "")

        if cid == "btn_create_event":
            await self._handle_create_event(interaction)
        elif cid.startswith("start_event_"):
            event_id = cid[len("start_event_"):]
            await self._handle_start_event(interaction, event_id)
        elif cid.startswith("end_event_"):
            event_id = cid[len("end_event_"):]
            await self._handle_end_event(interaction, event_id)

    # ── Create Event ──────────────────────────────────────────────────────────

    async def _handle_create_event(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado. Use `/setup`."), ephemeral=True
            )

        # Permission: admin or content creator
        admin_id = config["admin_role_id"]
        cc_id = config["cc_role_id"]
        user_roles = interaction.user.roles
        allowed = False
        for rid in [admin_id, cc_id]:
            if rid:
                role = interaction.guild.get_role(int(rid))
                if role and role in user_roles:
                    allowed = True
                    break

        if not allowed:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Apenas administradores ou criadores de conteúdo podem criar eventos."
                ),
                ephemeral=True,
            )

        # Show name modal — creation continues in _do_create_event
        await interaction.response.send_modal(EventNameModal(cog=self, config=config))

    async def _do_create_event(
        self, interaction: discord.Interaction, event_name: str, config
    ):
        """Actually create the event after the user provided a name via the modal."""
        await interaction.response.defer(ephemeral=True)

        event_id = str(uuid.uuid4())
        slug = _slug(event_name)

        # Get or fallback category
        category = None
        if config["event_category_id"]:
            category = interaction.guild.get_channel(int(config["event_category_id"]))

        # Create voice channel named after the event
        voice_channel = await interaction.guild.create_voice_channel(
            f"⚔️-{slug}",
            category=category,
            reason=f"Evento '{event_name}' criado por {interaction.user}",
        )

        # Send announcement in the current channel
        create_channel = interaction.channel
        embed = event_announce_embed(event_id[:8], interaction.user, voice_channel, event_name, participant_ids=[])
        view = _start_event_view(event_id)
        msg = await create_channel.send(embed=embed, view=view)

        # Bot adds skull reaction so users can react
        await msg.add_reaction("💀")

        # Persist event
        await create_event(
            event_id=event_id,
            guild_id=interaction.guild_id,
            voice_channel_id=voice_channel.id,
            announce_msg_id=msg.id,
            event_name=event_name,
        )

        await interaction.followup.send(
            embed=success_embed(
                f"Evento **{event_name}** (`{event_id[:8]}`) criado!\n"
                f"Canal de voz: {voice_channel.mention}"
            ),
            ephemeral=True,
        )

    async def _get_or_create_finished_category(
        self, guild: discord.Guild
    ) -> discord.CategoryChannel:
        """Return the 'Eventos Finalizados' category, creating it if needed."""
        target = "Eventos Finalizados"
        for cat in guild.categories:
            if cat.name.lower() == target.lower():
                return cat
        return await guild.create_category(target)

    # ── Start Event ─────────────────────────────────────────────────────────────────────────────

    async def _handle_start_event(self, interaction: discord.Interaction, event_id: str):
        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado."), ephemeral=True
            )

        admin_id = config["admin_role_id"]
        cc_id = config["cc_role_id"]
        allowed = any(
            (rid and interaction.guild.get_role(int(rid)) in interaction.user.roles)
            for rid in [admin_id, cc_id]
        )
        if not allowed:
            return await interaction.response.send_message(
                embed=error_embed("Apenas administradores ou criadores de conteúdo podem iniciar eventos."),
                ephemeral=True,
            )

        event = await get_event(event_id)
        if not event:
            return await interaction.response.send_message(
                embed=error_embed("Evento não encontrado."), ephemeral=True
            )
        if event["status"] != "pending":
            return await interaction.response.send_message(
                embed=error_embed("Este evento já foi iniciado ou encerrado."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        now = time.time()
        await update_event(event_id, status="active", started_at=now)

        event_name = event["event_name"] or event_id[:8]

        # Set join_time for everyone already in the voice channel at start time
        voice_ch = interaction.guild.get_channel(int(event["voice_channel_id"])) if event["voice_channel_id"] else None
        if voice_ch:
            for m in voice_ch.members:
                if not m.bot:
                    await add_participant(event_id, m.id)
                    await participant_join_voice(event_id, m.id, now)

        # Edit the announce message: switch to 'Encerrar' button + refresh participants
        try:
            announce_ch = (
                interaction.guild.get_channel(int(config["create_event_channel_id"]))
                if config["create_event_channel_id"] else None
            )
            if announce_ch:
                ann_msg = await announce_ch.fetch_message(int(event["announce_msg_id"]))
                all_parts = await get_participants(event_id)
                ids = [int(p["user_id"]) for p in all_parts]
                if ann_msg.embeds:
                    old_e = ann_msg.embeds[0]
                    new_e = discord.Embed.from_dict(old_e.to_dict())
                    new_e.clear_fields()
                    for f in old_e.fields:
                        if not f.name.startswith("👥 Participantes"):
                            new_e.add_field(name=f.name, value=f.value, inline=f.inline)
                    if ids:
                        mentions = " ".join(f"<@{uid}>" for uid in ids[:25])
                        if len(ids) > 25:
                            mentions += f"\n… e mais {len(ids) - 25}"
                    else:
                        mentions = "—"
                    new_e.add_field(
                        name=f"👥 Participantes ({len(ids)})",
                        value=mentions,
                        inline=False,
                    )
                    await ann_msg.edit(embed=new_e, view=_end_event_view(event_id))
                else:
                    await ann_msg.edit(view=_end_event_view(event_id))
        except Exception:
            pass

        await audit_log(
            self.bot, interaction.guild_id,
            f"⚔️ Evento **{event_name}** (`{event_id[:8]}`) iniciado por {interaction.user.mention}."
        )
        await interaction.followup.send(
            embed=success_embed(f"Evento **{event_name}** iniciado! Contagem de participação começou."),
            ephemeral=True,
        )

    # ── End Event ─────────────────────────────────────────────────────────────

    async def _handle_end_event(self, interaction: discord.Interaction, event_id: str):
        config = await get_guild_config(interaction.guild_id)
        if not config:
            return await interaction.response.send_message(
                embed=error_embed("Bot não configurado."), ephemeral=True
            )

        # Permission: admin or cc
        admin_id = config["admin_role_id"]
        cc_id = config["cc_role_id"]
        user_roles = interaction.user.roles
        allowed = False
        for rid in [admin_id, cc_id]:
            if rid:
                role = interaction.guild.get_role(int(rid))
                if role and role in user_roles:
                    allowed = True
                    break

        if not allowed:
            return await interaction.response.send_message(
                embed=error_embed("Apenas administradores ou criadores de conteúdo podem encerrar eventos."),
                ephemeral=True,
            )

        event = await get_event(event_id)
        if not event:
            return await interaction.response.send_message(
                embed=error_embed("Evento não encontrado."), ephemeral=True
            )
        if event["status"] not in ("pending", "active"):
            return await interaction.response.send_message(
                embed=error_embed("Este evento já foi encerrado."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        ended_at = time.time()
        started_at = event["started_at"]
        # If event was never started, duration = 0 and no time-based pcts
        duration = (ended_at - started_at) if started_at else 0

        # Flush tracking for anyone still in the channel
        participants = await get_participants(event_id)
        for p in participants:
            if p["join_time"] is not None:
                await participant_leave_voice(event_id, int(p["user_id"]), ended_at)

        # Compute participation percentages based on time
        await finalize_participation_pcts(event_id, duration)

        # Resolve event name for channel naming
        event_name = event["event_name"] if event["event_name"] else event_id[:8]
        slug = _slug(event_name)

        # Delete the voice channel
        voice_channel = interaction.guild.get_channel(int(event["voice_channel_id"]))
        if voice_channel:
            try:
                await voice_channel.delete(reason=f"Evento '{event_name}' encerrado")
            except Exception:
                pass

        # Create text channel in "Eventos Finalizados" category
        category = await self._get_or_create_finished_category(interaction.guild)

        # Permissions for text channel: admin sends, others read-only
        admin_role = interaction.guild.get_role(int(admin_id)) if admin_id else None
        seller_role = (
            interaction.guild.get_role(int(config["seller_role_id"]))
            if config["seller_role_id"] else None
        )

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
            ),
        }
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                manage_messages=True,
            )
        if seller_role:
            overwrites[seller_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
            )

        # Create text channel for split commands
        short_id = event_id[:8]
        text_channel = await interaction.guild.create_text_channel(
            f"📋-{slug}",
            category=category,
            overwrites=overwrites,
            topic=f"Evento '{event_name}' ({short_id}). Use os comandos aqui para finalizar.",
        )

        # Update event record
        await update_event(event_id, status="ended", ended_at=ended_at,
                           text_channel_id=str(text_channel.id))

        # Re-fetch participants with updated percentages
        participants = await get_participants(event_id)
        tax_rate = config["tax_rate"] or 0
        embed = event_summary_embed(event_id, participants, duration, tax_rate=tax_rate, event_name=event_name)

        await text_channel.send(
            content=(
                f"🔒 **{event_name}** (`{short_id}`) encerrado por {interaction.user.mention}\n"
                "Use `/setar-prata` para definir a prata obtida, depois `/simular` para calcular o split."
            ),
            embed=embed,
        )

        # Delete the announce message so the participation channel stays clean
        try:
            announce_channel = (
                interaction.guild.get_channel(int(config["create_event_channel_id"]))
                if config["create_event_channel_id"] else None
            )
            if announce_channel:
                ann_msg = await announce_channel.fetch_message(int(event["announce_msg_id"]))
                await ann_msg.delete()
        except Exception:
            pass

        await audit_log(
            self.bot, interaction.guild_id,
            f"⛏️ Evento `{short_id}` encerrado por {interaction.user.mention}. "
            f"Duração: {int(duration // 60)}m {int(duration % 60)}s."
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Evento `{short_id}` encerrado! Canal: {text_channel.mention}"
            ),
            ephemeral=True,
        )

    # ── Reaction: join event ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != "💀":
            return

        # Find event by announce message id
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        events = await get_all_active_events()
        event = next(
            (e for e in events if str(e["announce_msg_id"]) == str(payload.message_id)),
            None,
        )
        if not event:
            return

        # Check if user has at least 'member' role — ALWAYS required
        config = await get_guild_config(payload.guild_id)
        if not config:
            return  # Bot not configured; ignore reactions

        member_role_id = config["member_role_id"]
        admin_role_id = config["admin_role_id"]
        cc_role_id = config["cc_role_id"]
        seller_role_id = config["seller_role_id"]

        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return

        allowed_rids = [member_role_id, admin_role_id, cc_role_id, seller_role_id]
        has_role = any(
            guild.get_role(int(rid)) in member.roles
            for rid in allowed_rids if rid
        )
        if not has_role:
            return

        # Register participant
        await add_participant(event["id"], payload.user_id)

        # Schedule a debounced embed refresh (collapses burst reactions into 1 HTTP call)
        self._schedule_embed_refresh(event, guild, config)

        voice_ch = guild.get_channel(int(event["voice_channel_id"]))
        if member.voice and member.voice.channel:
            try:
                await member.move_to(voice_ch)
            except discord.Forbidden:
                pass
        else:
            # User is not in any voice channel; send DM
            try:
                await member.send(
                    f"⚔️ Você foi registrado no evento `{event['id'][:8]}`!\n"
                    f"Entre no canal de voz **{voice_ch.name}** para ter sua participação contada."
                )
            except discord.Forbidden:
                pass

    # ── Voice State: track participation time ─────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        # Collect all ACTIVE (started) event voice channel IDs for this guild
        events = await get_all_active_events()
        guild_events = [
            e for e in events
            if str(e["guild_id"]) == str(member.guild.id) and e["status"] == "active"
        ]

        voice_ids = {str(e["voice_channel_id"]): e for e in guild_events}

        now = time.time()

        # User joined an event channel
        if after.channel and str(after.channel.id) in voice_ids:
            event = voice_ids[str(after.channel.id)]
            # Ensure they're registered as participant (in case they skipped reaction)
            await add_participant(event["id"], member.id)
            await participant_join_voice(event["id"], member.id, now)

        # User left an event channel
        if before.channel and str(before.channel.id) in voice_ids:
            event = voice_ids[str(before.channel.id)]
            await participant_leave_voice(event["id"], member.id, now)

    # ── New commands ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="eventos-ativos",
        description="Lista todos os eventos em andamento neste servidor. (Admin)",
    )
    @is_admin()
    async def eventos_ativos(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        events = await get_active_events(interaction.guild_id)
        if not events:
            return await interaction.followup.send(
                embed=success_embed("Nenhum evento ativo no momento."), ephemeral=True
            )

        embed = discord.Embed(
            title="⚔️  Eventos Ativos",
            colour=discord.Colour.gold(),
        )
        now = time.time()
        for ev in events:
            voice_ch = interaction.guild.get_channel(int(ev["voice_channel_id"])) if ev["voice_channel_id"] else None
            duration = int(now - (ev["started_at"] or now))
            minutes, seconds = duration // 60, duration % 60
            ev_name = ev["event_name"] or ev["id"][:8]
            embed.add_field(
                name=f"⚔️  {ev_name}  (`{ev['id'][:8]}`)",
                value=(
                    f"Canal: {voice_ch.mention if voice_ch else 'Removido'}\n"
                    f"Em andamento há: `{minutes}m {seconds}s`"
                ),
                inline=False,
            )
        embed.set_footer(text="Velho Covil Bot")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="reset-evento",
        description="Cancela um evento sem distribuir prata. (Admin)",
    )
    @app_commands.describe(event_id="ID curto do evento (primeiros 8 caracteres)")
    @is_admin()
    async def reset_evento(self, interaction: discord.Interaction, event_id: str):
        await interaction.response.defer(ephemeral=True)

        # Find event by short ID prefix
        events = await get_active_events(interaction.guild_id)
        matched = [e for e in events if e["id"].startswith(event_id.lower())]
        if not matched:
            return await interaction.followup.send(
                embed=error_embed(f"Nenhum evento ativo com ID começando com `{event_id}`."),
                ephemeral=True,
            )
        if len(matched) > 1:
            return await interaction.followup.send(
                embed=error_embed("ID ambíguo — use mais caracteres do ID do evento."),
                ephemeral=True,
            )

        ev = matched[0]
        full_id = ev["id"]
        short = full_id[:8]

        confirm_view = discord.ui.View(timeout=60)
        confirm_view.add_item(
            discord.ui.Button(
                label="✅  Confirmar Cancelamento",
                style=discord.ButtonStyle.danger,
                custom_id=f"reset_confirm_{full_id}",
            )
        )
        confirm_view.add_item(
            discord.ui.Button(
                label="Cancelar",
                style=discord.ButtonStyle.secondary,
                custom_id=f"reset_abort_{full_id}",
            )
        )
        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"⚠️ Você está prestes a **cancelar** o evento `{short}` sem distribuir prata.\n"
                    "Os canais serão removidos e os participantes serão descartados.\n\n"
                    "Tem certeza?"
                ),
                colour=discord.Colour.orange(),
            ),
            view=confirm_view,
            ephemeral=True,
        )

    @commands.Cog.listener("on_interaction")
    async def _reset_confirm_listener(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid: str = interaction.data.get("custom_id", "")

        if cid.startswith("reset_confirm_"):
            full_id = cid[len("reset_confirm_"):]
            await interaction.response.defer(ephemeral=True)

            ev = await get_event(full_id)
            if not ev or ev["status"] not in ("pending", "active"):
                return await interaction.followup.send(
                    embed=error_embed("Evento não encontrado ou já encerrado."), ephemeral=True
                )

            # Cancel the event
            await update_event(full_id, status="cancelled")

            # Delete voice channel
            event_name = ev["event_name"] if ev["event_name"] else full_id[:8]
            voice_ch = interaction.guild.get_channel(int(ev["voice_channel_id"])) if ev["voice_channel_id"] else None
            if voice_ch:
                try:
                    await voice_ch.delete(reason=f"Evento '{event_name}' cancelado")
                except Exception:
                    pass

            await audit_log(
                self.bot, interaction.guild_id,
                f"🗑️ Evento `{full_id[:8]}` cancelado por {interaction.user.mention} (sem distribuição de prata)."
            )
            await interaction.followup.send(
                embed=success_embed(f"Evento `{full_id[:8]}` cancelado com sucesso."), ephemeral=True
            )

        elif cid.startswith("reset_abort_"):
            await interaction.response.send_message(
                embed=success_embed("Cancelamento abortado."), ephemeral=True
            )

    @app_commands.command(
        name="deletar-evento",
        description="Deleta o canal de texto de um evento encerrado. (Admin)",
    )
    @is_admin()
    async def deletar_evento(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Use interaction.channel.id — more reliable than interaction.channel_id in py-cord 2.x
        # Don't filter by status in the DB query; check in Python so we also
        # accept "cancelled" events and get a correct match regardless of state.
        event = await get_event_by_channel(interaction.channel.id)
        if not event or event["status"] not in ("ended", "cancelled"):
            return await interaction.followup.send(
                embed=error_embed("Este canal não é um canal de evento encerrado."),
                ephemeral=True,
            )

        event_name = event["event_name"] or event["id"][:8]
        channel = interaction.channel
        await audit_log(
            self.bot, interaction.guild_id,
            f"🗑️ Canal do evento **{event_name}** deletado por {interaction.user.mention}.",
        )
        await interaction.followup.send(
            embed=success_embed(f"Canal do evento **{event_name}** será deletado."),
            ephemeral=True,
        )
        await channel.delete(reason=f"Evento '{event_name}' — canal deletado por {interaction.user}")

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(embed=error_embed(str(error)), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
