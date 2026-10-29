"""Direct theft registration and durable publication with private error recovery."""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import discord
from discord.ext import commands

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.assets import UploadedAsset
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.stolen_weapons.models import (
    MAX_EVIDENCE_IMAGES,
    StolenWeaponReport,
    TheftDetails,
)
from arrest_report_agent.stolen_weapons.service import StolenWeaponService, validate_images

LOGGER = logging.getLogger(__name__)
FORM_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class RegistrationContext:
    requester_id: int
    guild_id: int
    channel_id: int
    destination_channel_id: int

    def matches(self, interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild is not None
            and interaction.guild.id == self.guild_id
            and interaction.channel is not None
            and interaction.channel.id == self.channel_id
            and interaction.user is not None
            and interaction.user.id == self.requester_id
        )


def _text(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def build_theft_embeds(
    details: TheftDetails,
    *,
    registered_by: int,
    files: tuple[UploadedAsset, ...] = (),
    reference: str | None = None,
    created_at: datetime | None = None,
    closed_report: StolenWeaponReport | None = None,
    image_names: tuple[str, ...] = (),
) -> list[discord.Embed]:
    embed = discord.Embed(
        title="🔫 Registro de robo de arma", color=discord.Color.orange(), timestamp=created_at
    )
    embed.add_field(name="Serial", value=_text(details.serial), inline=True)
    embed.add_field(name="Fecha del robo", value=details.stolen_on.strftime("%d/%m/%Y"), inline=True)
    embed.add_field(name="Propietario / víctima", value=_text(details.victim_name), inline=False)
    description = _text(details.description)
    for start in range(0, len(description), 1000):
        embed.add_field(
            name="Hechos" if start == 0 else "Hechos (continuación)",
            value=description[start : start + 1000],
            inline=False,
        )
    embed.add_field(name="Registrado por", value=f"<@{registered_by}>", inline=False)
    closed = closed_report is not None and closed_report.status == "closed"
    embed.add_field(name="Estado", value="Cerrado" if closed else "Abierto", inline=False)
    if closed and closed_report is not None:
        embed.title = "✅ Reporte de robo cerrado"
        embed.color = discord.Color.green()
        embed.add_field(name="Cerrado por", value=f"<@{closed_report.closed_by}>", inline=True)
        if closed_report.closed_at is not None:
            embed.add_field(
                name="Fecha de cierre", value=f"<t:{int(closed_report.closed_at.timestamp())}:f>", inline=True
            )
        embed.add_field(
            name="Motivo del cierre", value=_text(closed_report.closure_reason or ""), inline=False
        )
    embed.set_footer(text=reference or "Registro de robo de arma")
    embeds = [embed]
    names = image_names or tuple(item.filename for item in files)
    for index, filename in enumerate(names):
        image_embed = embed if index == 0 else discord.Embed(color=embed.color)
        image_embed.set_image(url=f"attachment://{filename}")
        if index:
            embeds.append(image_embed)
    return embeds


def _discord_files(files: tuple[UploadedAsset, ...]) -> list[discord.File]:
    return [discord.File(io.BytesIO(item.payload), filename=item.filename) for item in files]


async def _error(interaction: discord.Interaction, description: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            description, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await interaction.response.send_message(
            description,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class StolenWeaponCog(commands.Cog):
    def __init__(self, provider: DatabaseConfigProvider, service: StolenWeaponService) -> None:
        self.provider = provider
        self.service = service

    async def authorize(
        self,
        guild: discord.Guild | None,
        channel: object,
        user: object,
        *,
        expected_destination: int | None = None,
    ) -> tuple[discord.TextChannel, int, int]:
        if (
            guild is None
            or not isinstance(channel, discord.TextChannel)
            or not isinstance(user, discord.Member)
        ):
            raise ValueError("Usa este comando dentro de uno de los canales habilitados del servidor.")
        bundle = await self.provider.cached(guild.id)
        options = bundle.settings.stolen_weapon_options
        if options is None:
            raise ValueError("El registro de armas robadas no está habilitado en este servidor.")
        if channel.id not in options.source_channel_ids:
            raise ValueError("Este canal no está habilitado para registrar robos de armas.")
        if user.bot or not set(bundle.settings.operational_staff_role_ids).intersection(
            role.id for role in user.roles
        ):
            raise ValueError("No tienes un rol habilitado para registrar robos de armas.")
        if not channel.permissions_for(user).view_channel:
            raise ValueError("No tienes acceso al canal de registro.")
        if expected_destination is not None and options.destination_channel_id != expected_destination:
            raise ValueError("El canal de destino cambió. Abre nuevamente el comando.")
        destination = guild.get_channel(options.destination_channel_id)
        if (
            not isinstance(destination, discord.TextChannel)
            or destination.guild.id != guild.id
            or guild.me is None
        ):
            raise ValueError("No pude acceder al canal de publicación configurado.")
        permissions = destination.permissions_for(guild.me)
        if not all(
            (
                permissions.view_channel,
                permissions.send_messages,
                permissions.embed_links,
                permissions.attach_files,
            )
        ):
            raise ValueError("El bot necesita ver el canal de destino, enviar mensajes, embeds y adjuntos.")
        return (
            destination,
            min(bundle.settings.max_file_bytes, bundle.settings.max_image_bytes, guild.filesize_limit),
            min(bundle.settings.max_total_file_bytes, 25 * 1024 * 1024),
        )

    async def authorize_form(self, interaction: discord.Interaction, context: RegistrationContext):
        if not context.matches(interaction):
            raise ValueError("Este formulario pertenece a otra persona o canal.")
        member = await interaction.guild.fetch_member(context.requester_id)
        return await self.authorize(
            interaction.guild,
            interaction.channel,
            member,
            expected_destination=context.destination_channel_id,
        )

    @discord.slash_command(
        name="registrar-robo-arma",
        description="Registra el robo de un arma con su serial y evidencias",
        contexts={discord.InteractionContextType.guild},
    )
    async def register_theft(self, ctx: discord.ApplicationContext) -> None:
        try:
            destination, _file_limit, _total_limit = await self.authorize(ctx.guild, ctx.channel, ctx.author)
            context = RegistrationContext(ctx.author.id, ctx.guild.id, ctx.channel.id, destination.id)
            await ctx.send_modal(TheftDetailsModal(self, context))
        except ValueError as error:
            await ctx.respond(str(error), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            log_event(
                LOGGER, "error", "stolen_weapon_form_open_failed", exc_info=error, **exception_fields(error)
            )
            await ctx.respond("No pude abrir el formulario. Prueba de nuevo más tarde.", ephemeral=True)

    @discord.slash_command(
        name="cerrar-reporte-robo",
        description="Cierra un reporte de robo de arma",
        contexts={discord.InteractionContextType.guild},
    )
    async def close_theft(self, ctx: discord.ApplicationContext) -> None:
        try:
            destination, _file_limit, _total_limit = await self.authorize(ctx.guild, ctx.channel, ctx.author)
            context = RegistrationContext(ctx.author.id, ctx.guild.id, ctx.channel.id, destination.id)
            await ctx.send_modal(TheftClosureModal(self, context))
        except ValueError as error:
            await ctx.respond(str(error), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            log_event(LOGGER, "error", "weapon_theft_closure_form_failed", **exception_fields(error))
            await ctx.respond("No pude abrir el cierre del reporte. Prueba de nuevo.", ephemeral=True)


class TheftClosureModal(discord.ui.DesignerModal):
    def __init__(self, cog: StolenWeaponCog, context: RegistrationContext) -> None:
        self.cog, self.context = cog, context
        self.reference = discord.ui.InputText(placeholder="ARM-A1B2C3D4E5F6", min_length=16, max_length=16)
        self.reason = discord.ui.InputText(style=discord.InputTextStyle.long, min_length=5, max_length=500)
        super().__init__(
            discord.ui.Label("Referencia del reporte de robo", self.reference),
            discord.ui.Label("Motivo del cierre", self.reason),
            title="Cerrar reporte de robo",
            timeout=FORM_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, invisible=False)
        report = None
        try:
            await self.cog.authorize_form(interaction, self.context)
            repository = self.cog.service.repository
            existing = await repository.get_by_reference(self.context.guild_id, self.reference.value or "")
            report, changed = await repository.close(
                self.context.guild_id,
                existing.id,
                user_id=self.context.requester_id,
                interaction_id=interaction.id,
                reason=self.reason.value or "",
            )
            if report.message_id is not None:
                # The report can predate a configured destination change; edit its actual original message.
                channel = interaction.guild.get_channel(report.destination_channel_id)
                if not isinstance(channel, discord.TextChannel):
                    raise RuntimeError("Original theft channel unavailable")
                message = await channel.fetch_message(report.message_id)
                await message.edit(
                    embeds=build_theft_embeds(
                        report.details,
                        registered_by=report.registered_by,
                        reference=report.reference,
                        created_at=report.created_at,
                        closed_report=report,
                        image_names=tuple(asset.filename for asset in report.assets),
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            log_event(
                LOGGER,
                "info",
                "weapon_theft_report_closed",
                report_id=str(report.id),
                guild_id=self.context.guild_id,
                user_id=self.context.requester_id,
                changed=changed,
            )
            await interaction.delete_original_response()
        except ValueError as error:
            await interaction.edit_original_response(
                content=str(error), allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "weapon_theft_closure_failed",
                guild_id=self.context.guild_id,
                report_id=str(report.id) if report else None,
                **exception_fields(error),
            )
            await interaction.edit_original_response(
                content=(
                    f"El reporte `{report.reference}` quedó cerrado, pero no pude actualizar su mensaje. "
                    "Puedes repetir el cierre para actualizarlo."
                    if report
                    else "No pude cerrar el reporte. Prueba de nuevo."
                )
            )


class TheftDetailsModal(discord.ui.DesignerModal):
    def __init__(self, cog: StolenWeaponCog, context: RegistrationContext) -> None:
        self.cog, self.context = cog, context
        self.serial = discord.ui.InputText(placeholder="Ej.: SA-001234", min_length=1, max_length=100)
        self.lock = asyncio.Lock()
        self.publication: TheftPublicationView | None = None
        self.description = discord.ui.InputText(
            style=discord.InputTextStyle.long, min_length=10, max_length=2000
        )
        self.stolen_on = discord.ui.InputText(placeholder="DD/MM/AAAA", min_length=10, max_length=10)
        self.victim = discord.ui.InputText(min_length=2, max_length=100)
        self.upload = discord.ui.FileUpload(min_values=1, max_values=MAX_EVIDENCE_IMAGES, required=True)
        super().__init__(
            discord.ui.Label("Serial del arma", self.serial),
            discord.ui.Label("Hechos / descripción", self.description),
            discord.ui.Label("Fecha del robo", self.stolen_on),
            discord.ui.Label("Nombre del propietario (víctima)", self.victim),
            discord.ui.Label(
                "Evidencias del robo", self.upload, description="De 1 a 5 imágenes · PNG, JPEG, WebP o GIF"
            ),
            title="Registrar robo de arma",
            timeout=FORM_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, invisible=False)
        async with self.lock:
            try:
                if self.publication is not None:
                    await self.publication.publish(interaction)
                    return
                _destination, file_limit, total_limit = await self.cog.authorize_form(
                    interaction, self.context
                )
                details = TheftDetails.parse(
                    serial=self.serial.value or "",
                    description=self.description.value or "",
                    stolen_on=self.stolen_on.value or "",
                    victim_name=self.victim.value or "",
                    reporting_officer_name=f"{interaction.user.display_name} ({interaction.user.id})",
                )
                attachments = tuple(cast(list[discord.Attachment], self.upload.values or []))
                if not 1 <= len(attachments) <= MAX_EVIDENCE_IMAGES:
                    raise ValueError("Adjunta entre 1 y 5 imágenes de evidencia.")
                if any(not 0 < item.size <= file_limit for item in attachments):
                    raise ValueError("Una evidencia está vacía o supera el tamaño permitido por imagen.")
                if sum(item.size for item in attachments) > total_limit:
                    raise ValueError("Las evidencias superan el tamaño total permitido.")
                async with asyncio.timeout(90):
                    files = tuple([
                        UploadedAsset(item.filename, item.content_type, await item.read())
                        for item in attachments
                    ])
                files = await asyncio.to_thread(
                    validate_images, files, max_file_bytes=file_limit, max_total_bytes=total_limit,
                )
                self.publication = TheftPublicationView(
                    self.cog, self.context, details, submission_id=interaction.id,
                )
                self.publication.files = files
                await self.publication.publish(interaction)
            except ValueError as error:
                await interaction.edit_original_response(
                    content=str(error), allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as error:
                log_event(
                    LOGGER, "error", "stolen_weapon_form_failed", interaction_id=interaction.id,
                    exc_info=error, **exception_fields(error),
                )
                await interaction.edit_original_response(
                    content="No pude procesar los datos o las evidencias. Vuelve a abrir el comando.",
                )


class TheftPublicationView(discord.ui.View):
    def __init__(
        self, cog: StolenWeaponCog, context: RegistrationContext, details: TheftDetails, *, submission_id: int
    ) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog, self.context, self.details = cog, context, details
        self.submission_id = submission_id
        self.files: tuple[UploadedAsset, ...] = ()
        self.report: StolenWeaponReport | None = None
        self.message = None
        self.closed = False
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.context.matches(interaction) or self.closed:
            await _error(
                interaction, "Este reintento ya no está disponible o pertenece a otra persona."
            )
            return False
        return True

    async def on_timeout(self) -> None:
        async with self.lock:
            self.closed = True
            self.files = ()
            if self.message is not None:
                with suppress(discord.HTTPException):
                    await self.message.edit(view=None)

    @discord.ui.button(label="Reintentar publicación", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def retry_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.publish(interaction)

    async def publish(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, invisible=False)
        async with self.lock:
            if self.closed:
                await interaction.delete_original_response()
                return
            try:
                destination, file_limit, total_limit = await self.cog.authorize_form(
                    interaction, self.context
                )
                if not self.files:
                    raise ValueError("Adjunta de 1 a 5 imágenes para registrar el robo.")
                if self.report is None:
                    self.report = await self.cog.service.register(
                        submission_id=self.submission_id,
                        guild_id=self.context.guild_id,
                        source_channel_id=self.context.channel_id,
                        destination_channel_id=destination.id,
                        registered_by=self.context.requester_id,
                        details=self.details,
                        files=self.files,
                        max_file_bytes=file_limit,
                        max_total_bytes=total_limit,
                    )
                report = self.report
                if report.publication_status == "published":
                    await self.finish()
                    await interaction.delete_original_response()
                    return
                # Read before claiming: a transient bucket read failure remains safe to retry.
                files = tuple(
                    [
                        UploadedAsset(
                            asset.filename,
                            asset.content_type,
                            await self.cog.service.storage.get(asset.object_key),
                        )
                        for asset in report.assets
                    ]
                )
                if any(len(item.payload) > file_limit for item in files) or (
                    sum(len(item.payload) for item in files) > total_limit
                ):
                    raise ValueError("Las evidencias guardadas superan los límites actuales de adjuntos.")
                claimed = await self.cog.service.repository.claim_publication(
                    self.context.guild_id, report.id
                )
                if not claimed:
                    await self.finish()
                    await interaction.edit_original_response(
                        content=(
                            f"El registro `{report.reference}` ya se está publicando. "
                            "Revisa el canal de destino."
                        ),
                        view=None,
                    )
                    return
                # Publish the persisted bytes, so the visible evidence matches the durable record.
                try:
                    published = await destination.send(
                        embeds=build_theft_embeds(
                            report.details,
                            registered_by=report.registered_by,
                            files=files,
                            reference=report.reference,
                            created_at=report.created_at,
                        ),
                        files=_discord_files(files),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, discord.NotFound) as error:
                    await self.cog.service.repository.mark_failed(self.context.guild_id, report.id)
                    raise ValueError(
                        "El registro quedó guardado, pero el bot no puede publicar en el destino. "
                        "Revisa sus permisos y reintenta la publicación de este mismo registro."
                    ) from error
                except Exception:
                    # A timeout/5xx may have delivered the message. Do not blindly publish it twice.
                    await self.finish()
                    raise
                try:
                    await self.cog.service.repository.mark_published(
                        self.context.guild_id, report.id, published.id
                    )
                except Exception as error:
                    log_event(
                        LOGGER,
                        "critical",
                        "stolen_weapon_message_link_failed",
                        report_id=str(report.id),
                        message_id=published.id,
                        destination_channel_id=destination.id,
                        exc_info=error,
                        **exception_fields(error),
                    )
                    await self.finish()
                    await interaction.edit_original_response(
                        content=(
                            "El registro se publicó, pero no pude guardar el enlace. "
                            f"Referencia: `{report.reference}`."
                        ),
                        view=None,
                    )
                    return
                log_event(
                    LOGGER,
                    "info",
                    "stolen_weapon_registered",
                    report_id=str(report.id),
                    guild_id=report.guild_id,
                    user_id=report.registered_by,
                    message_id=published.id,
                    evidence_count=len(report.assets),
                    submission_id=self.submission_id,
                )
                await self.finish()
                await interaction.delete_original_response()
            except ValueError as error:
                self.message = await interaction.edit_original_response(
                    content=str(error), view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "stolen_weapon_registration_failed",
                    guild_id=self.context.guild_id,
                    user_id=self.context.requester_id,
                    submission_id=self.submission_id,
                    report_id=str(self.report.id) if self.report else None,
                    exc_info=error,
                    **exception_fields(error),
                )
                text = (
                    f"El registro `{self.report.reference}` y las evidencias están guardados. "
                    "No pude confirmar su publicación; revisa el canal de destino "
                    "antes de volver a registrarlo."
                    if self.closed and self.report
                    else "No pude completar el registro. Puedes reintentar sin volver a cargar los datos."
                )
                self.message = await interaction.edit_original_response(
                    content=text, view=None if self.closed else self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    async def finish(self) -> None:
        self.closed = True
        self.files = ()
        self.stop()
