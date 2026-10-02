"""Persistent FTP availability cards and private scheduling/reservation forms."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from arrest_report_agent.application.maintenance import MaintenanceScheduler
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import GuildRuntimeBundle

from .availability_models import BLOCK_MINUTES, MAX_BLOCKS, AvailabilityOffer, AvailabilitySlot
from .availability_repository import AvailabilityRepository
from .coordination import build_coordination_embed
from .discord import FTP_TIMEZONE_OPTIONS, FtpRequestService, FtpRequestServiceRegistry, parse_ftp_schedule
from .models import FtpTrainingPhase

LOGGER = logging.getLogger(__name__)


def instructor_type(service: FtpRequestService, member: discord.Member | None) -> str | None:
    """Resolve the highest configured instructor role, independently of Discord role order."""
    ftp, options = service.settings.ftp_status_options, service.request_options
    if member is None or ftp is None or options is None:
        return None
    member_roles = {role.id for role in member.roles}
    for label, role_ids in (
        ("Executive Staff", options.executive_role_ids),
        ("Command Staff", options.command_role_ids),
        ("Supervisory Staff", ftp.supervisory_role_ids),
        ("MFTO", ftp.mfto_role_ids),
        ("FTO", ftp.fto_role_ids),
    ):
        if member_roles.intersection(role_ids):
            return label
    return None


def can_offer(service: FtpRequestService, member: discord.Member) -> bool:
    return instructor_type(service, member) is not None


def can_reserve(service: FtpRequestService, member: discord.Member) -> bool:
    ftp = service.settings.ftp_status_options
    return bool(ftp and any(role.id in ftp.ftt_role_ids for role in member.roles))


def can_close(service: FtpRequestService, member: discord.Member, offer: AvailabilityOffer) -> bool:
    options = service.request_options
    return member.id == offer.instructor_id or bool(
        options and any(role.id in options.management_role_ids for role in member.roles)
    )


def safe_text(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def slot_time(slot: AvailabilitySlot) -> str:
    return f"<t:{int(slot.starts_at.timestamp())}:f> → <t:{int(slot.ends_at.timestamp())}:t>"


def build_offer_embed(
    offer: AvailabilityOffer,
    member: discord.Member | None = None,
    *,
    instructor_label: str | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    now = now or datetime.now(UTC)
    available = offer.available_slots(now)
    if offer.status == "closed":
        state, color = "🔒 Disponibilidad cerrada", discord.Color.light_grey()
    elif available:
        state, color = "Selecciona un bloque para reservar tu ficha.", discord.Color.green()
    elif offer.slots and all(slot.status == "reserved" for slot in offer.slots):
        state, color = "🎟️ Todos los bloques reservados", discord.Color.light_grey()
    elif offer.slots and all(
        slot.status == "expired" or (slot.status == "free" and slot.ends_at <= now) for slot in offer.slots
    ):
        state, color = "⌛ Disponibilidad vencida", discord.Color.light_grey()
    else:
        state, color = "📋 Sin bloques disponibles", discord.Color.light_grey()
    embed = discord.Embed(
        title="👨‍🏫 Disponibilidad para fichas",
        color=color,
        description=(
            f"**Instructor:** <@{offer.instructor_id}>\n"
            f"**Tipo:** {instructor_label or 'No disponible'}\n{state}"
        ),
    )
    if member is not None:
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    for slot in offer.slots:
        if slot.status == "reserved":
            phase = slot.phase.label if slot.phase else "Ficha"
            detail = f"✅ <@{slot.trainee_id}> · {phase}"
        elif slot in available:
            detail = "🗓️ Disponible"
        elif slot.status == "closed":
            detail = "Cerrado"
        else:
            detail = "Vencido"
        embed.add_field(name=f"Bloque {slot.position}", value=f"{slot_time(slot)}\n{detail}", inline=False)
    if offer.comments:
        embed.add_field(name="Comentarios", value=safe_text(offer.comments), inline=False)
    embed.set_footer(text="Field Training Program · Los horarios se muestran en tu hora local.")
    return embed


def build_booking_embed(offer: AvailabilityOffer, slot: AvailabilitySlot) -> discord.Embed:
    return build_coordination_embed(
        trainee_id=slot.trainee_id,
        instructor_id=offer.instructor_id,
        phase=slot.phase,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        block_position=slot.position,
        comments=slot.comments,
    )


async def error_response(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def clear_ephemeral_confirmation(interaction: discord.Interaction) -> None:
    """Only remove a separate private response, never the component's source card."""
    with suppress(discord.HTTPException):
        response = await interaction.original_response()
        source_id = interaction.message.id if interaction.message is not None else None
        if not response.flags.ephemeral or response.id == source_id:
            log_event(
                LOGGER,
                "warning",
                "ftp_availability_confirmation_cleanup_skipped",
                interaction_id=interaction.id,
                response_message_id=response.id,
                source_message_id=source_id,
            )
            return
        await interaction.delete_original_response()


class AvailabilityService:
    def __init__(
        self,
        bot: discord.Bot,
        registry: FtpRequestServiceRegistry,
        repository: AvailabilityRepository,
    ) -> None:
        self.bot, self.registry, self.repository = bot, registry, repository
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def context(self, interaction: discord.Interaction) -> tuple[FtpRequestService, discord.Member]:
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            raise ValueError("Usa esta opción dentro del servidor.")
        return await self.registry.get(interaction.guild_id), interaction.user

    async def open_form(self, interaction: discord.Interaction) -> None:
        try:
            service, member = await self.context(interaction)
            if not can_offer(service, member):
                raise ValueError("Solo FTO, MFTO, Supervisory, Command o Executive pueden ofrecer fichas.")
            if not service.is_command_channel(interaction.channel_id or 0):
                raise ValueError(f"Usa esta opción en {service.command_channel_mentions()}.")
            await interaction.response.send_message(
                "Elige tu zona horaria para programar bloques, o publica tu disponibilidad desde ahora.",
                view=AvailabilitySetupView(self, member.id),
                ephemeral=True,
            )
        except (ValueError, LookupError) as error:
            await error_response(interaction, str(error))

    async def create(
        self,
        interaction: discord.Interaction,
        *,
        start: datetime,
        minutes: int,
        count: int,
        comments: str,
    ) -> None:
        offer: AvailabilityOffer | None = None
        message: discord.Message | None = None
        published = False
        try:
            service, member = await self.context(interaction)
            if not can_offer(service, member):
                raise ValueError("Ya no tienes un rol habilitado para ofrecer fichas.")
            if not service.is_command_channel(interaction.channel_id or 0):
                raise ValueError(f"Usa esta opción en {service.command_channel_mentions()}.")
            # Modal submissions default to updating their source message in Pycord.
            # Create a separate private response before cleaning up the thinking state.
            await interaction.response.defer(ephemeral=True, invisible=False)
            async with self.lock(f"publish:{interaction.id}"):
                destination = await service.destination_channel(member.guild)
                ftp = service.settings.ftp_status_options
                assert ftp is not None
                roles = [role for role_id in ftp.ftt_role_ids if (role := member.guild.get_role(role_id))]
                if not roles:
                    raise ValueError("No se encontró el rol de trainee configurado. Avisa al staff.")
                offer = await self.repository.create(
                    guild_id=member.guild.id,
                    channel_id=destination.id,
                    interaction_id=interaction.id,
                    instructor_id=member.id,
                    start=start,
                    minutes=minutes,
                    count=count,
                    comments=comments,
                )
                if offer.status != "publishing":
                    raise ValueError("Esta publicación ya fue procesada. Revisa el canal de fichas.")
                # No clickable controls until the public message is durably associated in PostgreSQL.
                preview = offer.model_copy(update={"status": "open"})
                message = await destination.send(
                    content=" ".join(role.mention for role in roles),
                    embed=build_offer_embed(
                        preview, member, instructor_label=instructor_type(service, member)
                    ),
                    view=AvailabilityControlsView(disabled=True),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=False,
                        replied_user=False,
                        roles=[discord.Object(role.id) for role in roles],
                    ),
                )
                offer = await self.repository.publish(offer, message.id)
                published = True
            log_event(
                LOGGER,
                "info",
                "ftp_availability_published",
                guild_id=offer.guild_id,
                offer_id=str(offer.id),
                instructor_id=member.id,
                blocks=len(offer.slots),
            )
            await self.sync(offer)
            await clear_ephemeral_confirmation(interaction)
        except (ValueError, LookupError) as error:
            await error_response(interaction, str(error))
        except Exception as error:
            if offer is not None and not published and offer.status == "publishing":
                try:
                    await self.repository.fail_publication(offer)
                    if message is not None:
                        await message.delete()
                except Exception as cleanup_error:
                    log_event(
                        LOGGER,
                        "warning",
                        "ftp_availability_cleanup_failed",
                        offer_id=str(offer.id),
                        **exception_fields(cleanup_error),
                    )
            log_event(
                LOGGER,
                "error",
                "ftp_availability_publish_failed",
                guild_id=interaction.guild_id,
                published=published,
                **exception_fields(error),
            )
            await error_response(interaction, "No pude completar la publicación. Avisa al staff si persiste.")

    async def resolve(self, interaction: discord.Interaction) -> AvailabilityOffer:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            raise ValueError("No se encontró la tarjeta de disponibilidad.")
        return await self.repository.by_message(
            interaction.guild_id,
            interaction.channel_id,
            interaction.message.id,
        )

    async def select(self, interaction: discord.Interaction, raw_slot: str) -> None:
        try:
            service, member = await self.context(interaction)
            if not can_reserve(service, member):
                raise ValueError("Solo los Field Training Trainees pueden reservar bloques.")
            offer = await self.resolve(interaction)
            slot = next(
                (slot for slot in offer.available_slots(datetime.now(UTC)) if str(slot.id) == raw_slot), None
            )
            if slot is None:
                raise ValueError("Ese bloque ya no está disponible. Elige otro.")
            await interaction.response.send_modal(ReservationModal(self, member.id, offer, slot))
        except (ValueError, LookupError) as error:
            await error_response(interaction, str(error))

    async def reserve(
        self,
        interaction: discord.Interaction,
        offer_id: UUID,
        slot_id: UUID,
        phase: FtpTrainingPhase,
        comments: str,
    ) -> None:
        try:
            service, member = await self.context(interaction)
            if not can_reserve(service, member):
                raise ValueError("Solo los Field Training Trainees pueden reservar bloques.")
            # This modal belongs to the public offer card. Acknowledge it without
            # creating a confirmation; sync() edits the card and sends the booking reply.
            await interaction.response.defer(invisible=True)
            offer = await self.repository.get(member.guild.id, offer_id)
            if interaction.channel_id != offer.channel_id:
                raise ValueError("Esta reserva no corresponde a este canal.")
            instructor = member.guild.get_member(offer.instructor_id)
            if instructor is None:
                instructor = await member.guild.fetch_member(offer.instructor_id)
            if not can_offer(service, instructor):
                raise ValueError("El instructor ya no tiene un rol habilitado para ofrecer fichas.")
            offer = await self.repository.reserve(
                member.guild.id,
                offer_id,
                slot_id,
                trainee_id=member.id,
                phase=phase,
                comments=comments,
            )
            log_event(
                LOGGER,
                "info",
                "ftp_availability_reserved",
                guild_id=offer.guild_id,
                offer_id=str(offer.id),
                slot_id=str(slot_id),
                trainee_id=member.id,
            )
            await self.sync(offer)
        except (ValueError, LookupError) as error:
            await error_response(interaction, str(error))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "ftp_availability_reservation_failed",
                guild_id=interaction.guild_id,
                offer_id=str(offer_id),
                **exception_fields(error),
            )
            await error_response(
                interaction, "No pude completar la reserva. Revisa la tarjeta o avisa al staff."
            )

    async def close(self, interaction: discord.Interaction) -> None:
        try:
            service, member = await self.context(interaction)
            offer = await self.resolve(interaction)
            if not can_close(service, member, offer):
                raise ValueError("Solo el instructor, Command o Executive pueden cerrar esta disponibilidad.")
            # A deferred component update refers to the offer itself, not a temporary
            # response. Only edit the card; deleting the original response would erase it.
            await interaction.response.defer(invisible=True)
            offer = await self.repository.close(member.guild.id, offer.id)
            log_event(
                LOGGER,
                "info",
                "ftp_availability_closed",
                guild_id=offer.guild_id,
                offer_id=str(offer.id),
                actor_id=member.id,
            )
            await self.sync(offer)
        except (ValueError, LookupError) as error:
            await error_response(interaction, str(error))
        except Exception as error:
            log_event(LOGGER, "error", "ftp_availability_close_failed", **exception_fields(error))
            await error_response(interaction, "No pude cerrar la disponibilidad. Avisa al staff si persiste.")

    async def sync(self, snapshot: AvailabilityOffer) -> None:
        # Reload under a shared lock: callbacks and maintenance must not overwrite a newer card.
        async with self.lock(str(snapshot.id)):
            offer = await self.repository.get(snapshot.guild_id, snapshot.id)
            try:
                if offer.message_id is None:
                    return
                guild = self.bot.get_guild(offer.guild_id)
                if guild is None:
                    raise LookupError("Availability guild unavailable")
                channel = guild.get_channel(offer.channel_id) or await guild.fetch_channel(offer.channel_id)
                if not isinstance(channel, discord.TextChannel):
                    raise LookupError("Availability channel unavailable")
                message = await channel.fetch_message(offer.message_id)
                if self.bot.user is None or message.author.id != self.bot.user.id:
                    raise PermissionError("Availability message not owned by the bot")
                service = await self.registry.get(offer.guild_id)
                member = guild.get_member(offer.instructor_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(offer.instructor_id)
                    except discord.HTTPException as error:
                        # Missing member data must not prevent closing or updating the card.
                        log_event(
                            LOGGER,
                            "warning",
                            "ftp_availability_instructor_unavailable",
                            guild_id=offer.guild_id,
                            instructor_id=offer.instructor_id,
                            **exception_fields(error),
                        )
                embed = build_offer_embed(offer, member, instructor_label=instructor_type(service, member))
                if message.embeds and (author := message.embeds[0].to_dict().get("author")):
                    embed.set_author(name=author["name"], icon_url=author.get("icon_url"))
                await message.edit(
                    embed=embed,
                    view=AvailabilityControlsView(offer),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                for slot in offer.slots:
                    if not slot.notification_due:
                        continue
                    # Stable nonce + recent-history check recovers a send whose DB acknowledgement failed.
                    nonce = slot.id.hex[:25]
                    delivered = any(
                        [
                            item.author.id == self.bot.user.id and str(item.nonce) == nonce
                            async for item in channel.history(limit=50)
                        ]
                    )
                    if not delivered:
                        await message.reply(
                            content=f"<@{slot.trainee_id}> <@{offer.instructor_id}>",
                            embed=build_booking_embed(offer, slot),
                            nonce=nonce,
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False,
                                roles=False,
                                replied_user=False,
                                users=[
                                    discord.Object(slot.trainee_id or 0),
                                    discord.Object(offer.instructor_id),
                                ],
                            ),
                        )
                    await self.repository.notified(slot.id)
                await self.repository.acknowledge(offer)
            except discord.NotFound:
                # The card was deleted: do not leave invisible free slots blocking new offers.
                closed = await self.repository.close(offer.guild_id, offer.id)
                await self.repository.acknowledge(closed)
                log_event(LOGGER, "warning", "ftp_availability_card_missing", offer_id=str(offer.id))
            except Exception as error:
                await self.repository.acknowledge(offer, datetime.now(UTC) + timedelta(minutes=1))
                log_event(
                    LOGGER,
                    "warning",
                    "ftp_availability_sync_retry",
                    guild_id=offer.guild_id,
                    offer_id=str(offer.id),
                    **exception_fields(error),
                )

    async def configure_maintenance(
        self, scheduler: MaintenanceScheduler, bundle: GuildRuntimeBundle
    ) -> None:
        guild_id = bundle.snapshot.guild_id
        key = f"ftp-availability:{guild_id}"
        if bundle.settings.ftp_request_options is None:
            scheduler.unregister(key)
        else:
            scheduler.register(key, partial(self.maintain, guild_id), interval_seconds=60)

    async def maintain(self, guild_id: int) -> None:
        now = datetime.now(UTC)
        expired = await self.repository.expire(guild_id, now)
        for offer in await self.repository.due(guild_id, now):
            await self.sync(offer)
        if expired:
            log_event(LOGGER, "info", "ftp_availability_expired", guild_id=guild_id, blocks=expired)


def availability_service(interaction: discord.Interaction) -> AvailabilityService:
    service = getattr(interaction.client, "ftp_availability_service", None)
    if service is None:
        raise LookupError("La disponibilidad de fichas no está habilitada.")
    return cast(AvailabilityService, service)


class AvailabilitySelect(discord.ui.Select):
    def __init__(self, offer: AvailabilityOffer | None, disabled: bool) -> None:
        available = offer.available_slots(datetime.now(UTC)) if offer else ()
        super().__init__(
            custom_id="ftp-availability:v1:reserve",
            placeholder="Selecciona un bloque",
            min_values=1,
            max_values=1,
            disabled=disabled or not available,
            options=[
                discord.SelectOption(
                    label=f"Bloque {slot.position}",
                    value=str(slot.id),
                    description="Selecciona la fase antes de confirmar",
                )
                for slot in available
            ]
            or [discord.SelectOption(label="Sin bloques disponibles", value="none")],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await availability_service(interaction).select(interaction, (self.values or [""])[0])


class AvailabilityControlsView(discord.ui.View):
    def __init__(self, offer: AvailabilityOffer | None = None, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.add_item(AvailabilitySelect(offer, disabled))
        self.close_button.disabled = disabled or bool(offer and not offer.available_slots(datetime.now(UTC)))

    @discord.ui.button(
        label="Cerrar disponibilidad",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        custom_id="ftp-availability:v1:close",
        row=1,
    )
    async def close_button(
        self,
        _button: discord.ui.Button[AvailabilityControlsView],
        interaction: discord.Interaction,
    ) -> None:
        await availability_service(interaction).close(interaction)


class SetupTimezoneSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Tu zona horaria",
            options=[
                discord.SelectOption(label=key, value=key, emoji=emoji) for emoji, key in FTP_TIMEZONE_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(AvailabilitySetupView, self.view)
        view.timezone = (self.values or [""])[0]
        for option in self.options:
            option.default = option.value == view.timezone
        view.schedule.disabled = False
        await interaction.response.edit_message(view=view)


class AvailabilitySetupView(discord.ui.View):
    def __init__(self, service: AvailabilityService, user_id: int) -> None:
        super().__init__(timeout=300, disable_on_timeout=True)
        self.service, self.user_id = service, user_id
        self.timezone: str | None = None
        self.add_item(SetupTimezoneSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.user_id:
            return True
        await error_response(interaction, "Abre tu propio formulario para ofrecer fichas.")
        return False

    @discord.ui.button(label="Disponible ahora", emoji="⚡", row=1, style=discord.ButtonStyle.primary)
    async def now(
        self, _button: discord.ui.Button[AvailabilitySetupView], interaction: discord.Interaction
    ) -> None:
        await interaction.response.send_modal(AvailabilityModal(self.service, self.user_id, None))

    @discord.ui.button(label="Elegir horario", emoji="🗓️", row=1, disabled=True)
    async def schedule(
        self,
        _button: discord.ui.Button[AvailabilitySetupView],
        interaction: discord.Interaction,
    ) -> None:
        if self.timezone is not None:
            await interaction.response.send_modal(
                AvailabilityModal(self.service, self.user_id, self.timezone)
            )


class AvailabilityModal(discord.ui.DesignerModal):
    def __init__(self, service: AvailabilityService, user_id: int, timezone: str | None) -> None:
        self.service, self.user_id, self.timezone = service, user_id, timezone
        self.date = discord.ui.InputText(
            placeholder="DD/MM/AAAA",
            min_length=10,
            max_length=10,
            value=datetime.now(ZoneInfo(timezone or "UTC")).strftime("%d/%m/%Y"),
        )
        self.time = discord.ui.InputText(placeholder="HH:MM · formato 24 horas", min_length=4, max_length=5)
        self.duration = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(label=f"{minutes} minutos", value=str(minutes), default=minutes == 60)
                for minutes in BLOCK_MINUTES
            ],
            min_values=1,
            max_values=1,
        )
        self.count = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(
                    label=f"{count} bloque{'s' if count > 1 else ''}", value=str(count), default=count == 1
                )
                for count in range(1, MAX_BLOCKS + 1)
            ],
            min_values=1,
            max_values=1,
        )
        self.comments = discord.ui.InputText(
            style=discord.InputTextStyle.long, required=False, max_length=500
        )
        fields: list[discord.ui.Label[Any]] = []
        if timezone:
            fields.extend(
                [discord.ui.Label("Fecha", self.date), discord.ui.Label(f"Hora · {timezone}", self.time)]
            )
        fields.extend(
            [
                discord.ui.Label("Duración de cada bloque", self.duration),
                discord.ui.Label("Cantidad de bloques seguidos", self.count),
                discord.ui.Label("Comentarios (opcional)", self.comments),
            ]
        )
        super().__init__(*fields, title="Ofrecer fichas", timeout=300)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != self.user_id:
            await error_response(interaction, "Este formulario pertenece a otro usuario.")
            return
        try:
            start = (
                parse_ftp_schedule(self.date.value or "", self.time.value or "", self.timezone)
                if self.timezone
                else datetime.now(UTC)
            )
            minutes = int((self.duration.values or [""])[0])
            count = int((self.count.values or [""])[0])
        except (ValueError, IndexError):
            await error_response(interaction, "Revisa fecha (DD/MM/AAAA), hora (HH:MM), duración y cantidad.")
            return
        await self.service.create(
            interaction, start=start, minutes=minutes, count=count, comments=self.comments.value or ""
        )


class ReservationModal(discord.ui.DesignerModal):
    def __init__(
        self, service: AvailabilityService, user_id: int, offer: AvailabilityOffer, slot: AvailabilitySlot
    ) -> None:
        self.service, self.user_id, self.offer_id, self.slot_id = service, user_id, offer.id, slot.id
        self.phase = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(label=phase.label, value=phase.value) for phase in FtpTrainingPhase
            ],
            min_values=1,
            max_values=1,
        )
        self.comments = discord.ui.InputText(
            style=discord.InputTextStyle.long, required=False, max_length=500
        )
        super().__init__(
            discord.ui.Label("Ficha a realizar", self.phase),
            discord.ui.Label("Comentarios (opcional)", self.comments),
            title=f"Reservar bloque {slot.position}",
            timeout=300,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != self.user_id:
            await error_response(interaction, "Este formulario pertenece a otro usuario.")
            return
        try:
            phase = FtpTrainingPhase((self.phase.values or [""])[0])
        except (ValueError, IndexError):
            await error_response(interaction, "Selecciona una fase válida.")
            return
        await self.service.reserve(interaction, self.offer_id, self.slot_id, phase, self.comments.value or "")


class AvailabilityCog(commands.Cog):
    def __init__(self, service: AvailabilityService) -> None:
        self.service = service

    @discord.slash_command(
        name="ofrecer-ficha",
        description="Publica bloques disponibles para instruir trainees.",
        contexts={discord.InteractionContextType.guild},
    )
    async def offer_training(self, ctx: discord.ApplicationContext) -> None:
        await self.service.open_form(ctx.interaction)
