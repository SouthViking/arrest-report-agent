"""Discord command for validated, structured arrest-report submissions."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from arrest_report_agent.application.member_names import normalize_person_name
from arrest_report_agent.config import ArrestReportAttachmentMode
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.assets import AssetLimitError
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .attachments import build_discord_files
from .card import build_submission_view
from .models import ArrestReportSubmission, PublicationStatus
from .render import RenderedArrestReport, render_arrest_report_image
from .service import StructuredArrestReportService, StructuredReportFile

if TYPE_CHECKING:
    from arrest_report_agent.arrest_reports import SarpArrestReportLookup
    from arrest_report_agent.infrastructure.discord.cog import ReviewCog

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300


class StructuredArrestReportCog(commands.Cog):
    """Publish structured report roots while retaining the legacy mention workflow."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: StructuredArrestReportService,
        review_cog: ReviewCog,
    ) -> None:
        self.provider = provider
        self.service = service
        self.review_cog = review_cog
        self._initial_review_tasks: set[asyncio.Task[None]] = set()

    @discord.slash_command(
        name="revisar-arresto",
        description="Publica un reporte de arresto e inicia su revisión",
    )
    async def review_arrest(self, ctx: discord.ApplicationContext) -> None:
        denial = await self._command_denial(ctx)
        if denial is not None:
            await ctx.respond(embed=denial, ephemeral=True)
            return
        assert ctx.guild is not None and ctx.channel is not None
        bundle = await self.provider.cached(ctx.guild.id)
        await ctx.send_modal(
            StructuredArrestReportModal(
                cog=self,
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                generated_report_image=(
                    bundle.settings.arrest_report_attachment_mode is ArrestReportAttachmentMode.GENERATED
                ),
            )
        )

    async def publish(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        suspect_name: str,
        report_id_text: str,
        suspect_photos: tuple[discord.Attachment, ...],
        supporting_images: tuple[discord.Attachment, ...],
        generated_report_image: bool = False,
    ) -> None:
        if not _same_interaction_context(
            interaction,
            requester_id=requester_id,
            guild_id=guild_id,
            channel_id=channel_id,
        ):
            await interaction.response.send_message(
                embed=_alert("Este formulario ya no es válido."),
                ephemeral=True,
            )
            return
        assert isinstance(interaction.user, discord.Member)
        bundle = await self.provider.cached(guild_id)
        denial = _availability_denial(bundle, interaction.user, channel_id)
        if denial is not None:
            await interaction.response.send_message(embed=denial, ephemeral=True)
            return
        normalized_name = " ".join(suspect_name.split())
        raw_report_id = report_id_text.strip()
        if not normalized_name:
            await interaction.response.send_message(
                embed=_alert("Indica el nombre completo del sospechoso."),
                ephemeral=True,
            )
            return
        if not raw_report_id.isascii() or not raw_report_id.isdecimal():
            await interaction.response.send_message(
                embed=_alert("Indica un ID de arresto válido usando solo números."),
                ephemeral=True,
            )
            return
        report_id = int(raw_report_id)
        if report_id <= 0:
            await interaction.response.send_message(
                embed=_alert("Indica un ID de arresto válido mayor que cero."),
                ephemeral=True,
            )
            return
        valid_supporting_count = (
            0 <= len(supporting_images) <= 7 if generated_report_image else 1 <= len(supporting_images) <= 8
        )
        if not 1 <= len(suspect_photos) <= 2 or not valid_supporting_count:
            await interaction.response.send_message(
                embed=_alert(
                    "Adjunta entre 1 y 2 fotografías del sospechoso y hasta 7 respaldos adicionales."
                    if generated_report_image
                    else "Adjunta entre 1 y 2 fotografías del sospechoso y entre 1 y 8 respaldos."
                ),
                ephemeral=True,
            )
            return
        all_attachments = (*suspect_photos, *supporting_images)
        if (
            len(all_attachments) > 10
            or any(
                attachment.size <= 0
                or attachment.size > bundle.settings.max_file_bytes
                or not _is_supported_image(attachment)
                for attachment in all_attachments
            )
            or sum(attachment.size for attachment in all_attachments) > bundle.settings.max_total_file_bytes
        ):
            await interaction.response.send_message(
                embed=_alert("Una o más imágenes no son compatibles o superan el límite permitido."),
                ephemeral=True,
            )
            return
        lookup = cast("SarpArrestReportLookup | None", bundle.service.arrest_report_lookup)
        if lookup is None:
            await interaction.response.send_message(
                embed=_alert("La revisión de reportes no está disponible en este momento."),
                ephemeral=True,
            )
            return
        detail_result = await lookup.get(report_id)
        detail = detail_result.report
        if detail_result.error == "not_found" or (detail_result.error is None and detail is None):
            await interaction.response.send_message(
                embed=_alert(f"No existe un reporte de arresto en PCU con el ID #{report_id}."),
                ephemeral=True,
            )
            return
        if detail_result.error is not None or detail is None:
            await interaction.response.send_message(
                embed=_alert(
                    "No pude comprobar el ID de arresto en PCU en este momento. "
                    "Inténtalo nuevamente más tarde."
                ),
                ephemeral=True,
            )
            return
        if normalize_person_name(normalized_name) != normalize_person_name(detail.suspect_name):
            await interaction.response.send_message(
                embed=_alert(
                    f"El reporte #{report_id} corresponde a "
                    f"{detail.suspect_name.replace('_', ' ')}, no al sospechoso indicado."
                ),
                ephemeral=True,
            )
            return
        # The final report card replaces this public acknowledgement so Discord
        # keeps the publication associated with the slash command invocation.
        await interaction.response.defer(ephemeral=False, invisible=False)
        existing = await self.service.repository.get_by_interaction(interaction.id)
        if existing is not None and existing.publication_status is not PublicationStatus.FAILED:
            await _replace_deferred_with_ephemeral_error(
                interaction,
                _existing_embed(existing),
                guild_id=guild_id,
                user_id=requester_id,
            )
            return
        submission_suspect_name = detail.suspect_name.replace("_", " ")
        submission, created = await self.service.repository.reserve(
            guild_id=guild_id,
            interaction_id=interaction.id,
            owner_user_id=requester_id,
            parent_channel_id=channel_id,
            sarp_arrest_id=report_id,
            suspect_name=submission_suspect_name,
        )
        if not created:
            await _replace_deferred_with_ephemeral_error(
                interaction,
                _existing_embed(submission),
                guild_id=guild_id,
                user_id=requester_id,
            )
            return
        try:
            suspect_files = await _download_files(suspect_photos)
            supporting_files = await _download_files(supporting_images)
            rendered_report: RenderedArrestReport | None = None
            if generated_report_image:
                rendered_report = await asyncio.to_thread(render_arrest_report_image, detail)
                if (
                    len(rendered_report.payload) > bundle.settings.max_file_bytes
                    or sum(file.size for file in all_attachments) + len(rendered_report.payload)
                    > bundle.settings.max_total_file_bytes
                ):
                    raise AssetLimitError("El reporte generado supera el límite permitido")
            assets = await self.service.store_images(
                submission,
                suspect_photos=suspect_files,
                supporting_images=supporting_files,
                max_file_bytes=bundle.settings.max_file_bytes,
                max_total_bytes=bundle.settings.max_total_file_bytes,
            )
            output_files, suspect_names, supporting_names = build_discord_files(
                suspect_files,
                supporting_files,
                reserved_names=(
                    frozenset({rendered_report.filename}) if rendered_report is not None else frozenset()
                ),
            )
            if rendered_report is not None:
                output_files = (
                    *output_files,
                    discord.File(
                        io.BytesIO(rendered_report.payload),
                        filename=rendered_report.filename,
                    ),
                )
            refreshed_at = datetime.now(UTC) if rendered_report is not None else None
            root = await interaction.edit_original_response(
                view=build_submission_view(
                    submission,
                    suspect_filenames=suspect_names,
                    supporting_filenames=supporting_names,
                    report_filename=(rendered_report.filename if rendered_report is not None else None),
                    report_refreshed_at=refreshed_at,
                ),
                files=list(output_files),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            thread = await root.create_thread(
                name=_thread_name(submission.suspect_name, report_id),
                auto_archive_duration=bundle.settings.thread_auto_archive_minutes,
            )
            published = await self.service.repository.mark_published(
                submission.id,
                root_message_id=root.id,
                thread_id=thread.id,
            )
            if report_id is not None:
                binding_error = await lookup.bind(
                    guild_id=guild_id,
                    root_message_id=root.id,
                    parent_channel_id=channel_id,
                    thread_id=thread.id,
                    report_id=report_id,
                    actor_user_id=requester_id,
                    trigger_message_id=root.id,
                )
                if binding_error is not None:
                    log_event(
                        LOGGER,
                        "warning",
                        "structured_arrest_report_binding_failed",
                        guild_id=guild_id,
                        root_message_id=root.id,
                        thread_id=thread.id,
                        sarp_arrest_id=report_id,
                        binding_error=binding_error,
                    )
            task = asyncio.create_task(
                self.review_cog.run_structured_initial_review(
                    root=root,
                    thread=thread,
                    requester=interaction.user,
                    submission=published,
                    arrest_report=detail,
                    report_snapshot_current=rendered_report is not None,
                ),
                name=f"structured-arrest-review-{submission.id}",
            )
            self._initial_review_tasks.add(task)
            task.add_done_callback(self._initial_review_tasks.discard)
            log_event(
                LOGGER,
                "info",
                "structured_arrest_report_published",
                guild_id=guild_id,
                channel_id=channel_id,
                root_message_id=root.id,
                thread_id=thread.id,
                user_id=requester_id,
                sarp_arrest_id=report_id,
                asset_count=len(assets),
            )
        except (AssetLimitError, ValueError) as error:
            await self.service.repository.mark_publication_failed(submission.id)
            await _replace_deferred_with_ephemeral_error(
                interaction,
                _alert(str(error)),
                guild_id=guild_id,
                user_id=requester_id,
            )
        except Exception as error:
            await self.service.repository.mark_publication_failed(submission.id)
            log_event(
                LOGGER,
                "error",
                "structured_arrest_report_publication_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                sarp_arrest_id=report_id,
                **exception_fields(error),
            )
            await _replace_deferred_with_ephemeral_error(
                interaction,
                _alert("No pude publicar el reporte. Inténtalo nuevamente más tarde."),
                guild_id=guild_id,
                user_id=requester_id,
            )

    async def _command_denial(self, ctx: discord.ApplicationContext) -> discord.Embed | None:
        if ctx.guild is None or ctx.channel is None or not isinstance(ctx.author, discord.Member):
            return _alert("Este comando solo funciona dentro de un servidor.")
        bundle = await self.provider.cached(ctx.guild.id)
        return _availability_denial(bundle, ctx.author, ctx.channel.id)


class StructuredArrestReportModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        cog: StructuredArrestReportCog,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        generated_report_image: bool = False,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.generated_report_image = generated_report_image
        self.suspect_name = discord.ui.InputText(
            custom_id="arrest-report:v1:suspect",
            placeholder="Nombre Apellido",
            min_length=3,
            max_length=100,
        )
        self.report_id = discord.ui.InputText(
            custom_id="arrest-report:v1:id",
            placeholder="Ej.: 56242",
            min_length=1,
            max_length=20,
            required=True,
        )
        self.suspect_photos = discord.ui.FileUpload(
            custom_id="arrest-report:v1:suspect-photos",
            min_values=1,
            max_values=2,
            required=True,
        )
        self.supporting_images = discord.ui.FileUpload(
            custom_id="arrest-report:v1:supporting",
            min_values=0 if generated_report_image else 1,
            max_values=7 if generated_report_image else 8,
            required=not generated_report_image,
        )
        super().__init__(
            discord.ui.Label("Sospechoso", self.suspect_name),
            discord.ui.Label("ID del reporte SARP", self.report_id),
            discord.ui.Label(
                "Fotografías del sospechoso",
                self.suspect_photos,
                description="Frontal y lateral · 1 o 2 imágenes",
            ),
            discord.ui.Label(
                "Respaldos adicionales" if generated_report_image else "Reporte y respaldos",
                self.supporting_images,
                description=(
                    "Evidencia u otros antecedentes · opcional, hasta 7 imágenes"
                    if generated_report_image
                    else "Cargos, narrativa y otros respaldos · 1 a 8 imágenes"
                ),
            ),
            title="Revisar reporte de arresto",
            custom_id=f"arrest-report:v1:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            suspect_name=self.suspect_name.value or "",
            report_id_text=self.report_id.value or "",
            suspect_photos=tuple(cast(list[discord.Attachment], self.suspect_photos.values or [])),
            supporting_images=tuple(cast(list[discord.Attachment], self.supporting_images.values or [])),
            generated_report_image=self.generated_report_image,
        )


def _availability_denial(
    bundle: object,
    member: discord.Member,
    channel_id: int,
) -> discord.Embed | None:
    settings = bundle.settings  # type: ignore[attr-defined]
    feature = bundle.snapshot.features.get("arrest_report_lookup")  # type: ignore[attr-defined]
    if feature is None or not feature.enabled or settings.report_owner_access_max_rounds is None:
        return _alert("La revisión estructurada no está disponible en este servidor.")
    channels = settings.report_channel_allowlist.ids
    if channels is None or channel_id not in channels:
        channel_links = "\n".join(f"• <#{configured_id}>" for configured_id in sorted(channels or ()))
        description = "Este canal no está habilitado para revisar reportes."
        if channel_links:
            description += f" Usa `/revisar-arresto` en uno de estos canales:\n{channel_links}"
        return _alert(description)
    allowed_roles = set(settings.operational_staff_role_ids)
    if not allowed_roles or not any(role.id in allowed_roles for role in member.roles):
        return _alert("No tienes un rol habilitado para publicar reportes.")
    return None


def _same_interaction_context(
    interaction: discord.Interaction,
    *,
    requester_id: int,
    guild_id: int,
    channel_id: int,
) -> bool:
    return bool(
        interaction.user is not None
        and interaction.user.id == requester_id
        and interaction.guild is not None
        and interaction.guild.id == guild_id
        and interaction.channel is not None
        and interaction.channel.id == channel_id
    )


async def _download_files(
    attachments: tuple[discord.Attachment, ...],
) -> tuple[StructuredReportFile, ...]:
    files: list[StructuredReportFile] = []
    for attachment in attachments:
        files.append(
            StructuredReportFile(
                filename=attachment.filename,
                content_type=attachment.content_type,
                payload=await attachment.read(),
            )
        )
    return tuple(files)


def _is_supported_image(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").partition(";")[0].strip().casefold()
    return content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _thread_name(suspect_name: str, report_id: int | None) -> str:
    normalized_name = " ".join(suspect_name.split())
    suffix = f" · #{report_id}" if report_id is not None else ""
    return f"{normalized_name}{suffix}"[:100]


def _existing_embed(submission: ArrestReportSubmission) -> discord.Embed:
    if submission.jump_url is not None:
        description = f"Este envío ya fue publicado: {submission.jump_url}"
    elif submission.publication_status is PublicationStatus.PUBLISHING:
        description = "Este envío ya se está procesando. Espera un momento."
    else:
        description = "Este mismo envío ya fue procesado anteriormente."
    return discord.Embed(
        title="📋 Solicitud ya procesada",
        description=description,
        color=discord.Color.orange(),
    )


async def _replace_deferred_with_ephemeral_error(
    interaction: discord.Interaction,
    embed: discord.Embed,
    *,
    guild_id: int,
    user_id: int,
) -> None:
    """Remove the public acknowledgement before returning a private failure."""

    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException) as error:
        log_event(
            LOGGER,
            "debug",
            "structured_arrest_report_deferred_response_cleanup_failed",
            guild_id=guild_id,
            user_id=user_id,
            **exception_fields(error),
        )
    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _alert(description: str) -> discord.Embed:
    return discord.Embed(
        title="⚠️ No pude publicar el reporte",
        description=description,
        color=discord.Color.red(),
    )
