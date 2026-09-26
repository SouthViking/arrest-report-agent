"""Private image manager for published structured arrest-report threads."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

import discord

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.assets import AssetLimitError, UploadedAsset, safe_filename

from .attachments import build_discord_files
from .card import build_submission_view, is_submission_card
from .models import (
    ArrestReportAsset,
    ArrestReportAssetCategory,
    ArrestReportSubmission,
    PendingArrestReportAsset,
    PublicationStatus,
)
from .render import generated_report_filename
from .service import ALLOWED_IMAGE_TYPES, StructuredReportFile

if TYPE_CHECKING:
    from .discord import StructuredArrestReportCog

LOGGER = logging.getLogger(__name__)
Action = Literal["agregó", "reemplazó", "eliminó"]
AssetItem = ArrestReportAsset | PendingArrestReportAsset


def _asset_order(asset: AssetItem) -> tuple[int, int]:
    return (0 if asset.category is ArrestReportAssetCategory.SUSPECT_PHOTO else 1, asset.position)


def ordered_assets(submission: ArrestReportSubmission) -> tuple[ArrestReportAsset, ...]:
    return tuple(sorted(submission.assets, key=_asset_order))


def changed_assets(
    assets: tuple[ArrestReportAsset, ...],
    *,
    action: Action,
    category: ArrestReportAssetCategory,
    target_id: UUID | None = None,
    replacement: PendingArrestReportAsset | None = None,
) -> tuple[AssetItem, ...]:
    """Plan one edit without changing IDs of the unaffected images."""

    if action != "eliminó" and replacement is None:
        raise ValueError("Falta la imagen nueva.")
    if replacement is not None and replacement.category is not category:
        raise ValueError("La categoría de la imagen no coincide.")
    if action == "agregó":
        if target_id is not None:
            raise ValueError("No se puede añadir sobre una imagen existente.")
        result: tuple[AssetItem, ...] = (*assets, cast(PendingArrestReportAsset, replacement))
    else:
        index = next((i for i, asset in enumerate(assets) if asset.id == target_id), None)
        if index is None or assets[index].category is not category:
            raise ValueError("La imagen seleccionada ya no existe.")
        result = (
            *assets[:index],
            *((cast(PendingArrestReportAsset, replacement),) if action == "reemplazó" else ()),
            *assets[index + 1 :],
        )
    return tuple(sorted(result, key=_asset_order))


def validate_counts(assets: tuple[AssetItem, ...], *, generated_report: bool) -> None:
    suspect_count = sum(asset.category is ArrestReportAssetCategory.SUSPECT_PHOTO for asset in assets)
    support_count = len(assets) - suspect_count
    if not 1 <= suspect_count <= 2:
        raise ValueError("Debe quedar una o dos fotografías del sospechoso.")
    if not (0 if generated_report else 1) <= support_count <= (7 if generated_report else 8):
        raise ValueError("La cantidad de imágenes de respaldo no está permitida.")
    if len(assets) + int(generated_report) > 10:
        raise ValueError("Discord permite como máximo diez imágenes en el reporte.")


def can_manage_images(
    submission: ArrestReportSubmission,
    member: discord.Member,
    settings: object,
) -> bool:
    if member.id == submission.owner_user_id:
        return True
    management = getattr(settings, "ftp_request_options", None)
    leadership = set(getattr(settings, "supervisor_role_ids", ()))
    if management is not None:
        leadership.update(management.management_role_ids)
    warrants = getattr(settings, "warrant_notification_options", None)
    if warrants is not None:
        leadership.update(warrants.notification_role_ids)
    return bool(leadership.intersection(role.id for role in member.roles))


def _error(description: str) -> discord.Embed:
    return discord.Embed(
        title="No pude actualizar las imágenes", description=description, color=discord.Color.red()
    )


def _category_name(category: ArrestReportAssetCategory) -> str:
    return (
        "fotografía del sospechoso"
        if category is ArrestReportAssetCategory.SUSPECT_PHOTO
        else "imagen de respaldo"
    )


class ReportImageManager:
    def __init__(self, cog: StructuredArrestReportCog) -> None:
        self.cog = cog
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def resolve(
        self, interaction: discord.Interaction
    ) -> tuple[ArrestReportSubmission, discord.Thread, discord.Message] | None:
        if not isinstance(interaction.channel, discord.Thread) or interaction.guild_id is None:
            return None
        thread = interaction.channel
        submission = await self.cog.service.repository.get_by_thread(interaction.guild_id, thread.id)
        if (
            submission is None
            or submission.publication_status is not PublicationStatus.PUBLISHED
            or submission.root_message_id is None
            or submission.parent_channel_id != thread.parent_id
            or not isinstance(interaction.user, discord.Member)
        ):
            return None
        bundle = await self.cog.provider.cached(interaction.guild_id)
        feature = bundle.snapshot.features.get("arrest_report_lookup")
        if (
            feature is None
            or not feature.enabled
            or not can_manage_images(submission, interaction.user, bundle.settings)
        ):
            return None
        parent = thread.parent
        if not isinstance(parent, discord.TextChannel):
            return None
        root = await parent.fetch_message(submission.root_message_id)
        if not is_submission_card(root):
            return None
        return submission, thread, root

    async def open(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        try:
            resolved = await self.resolve(ctx.interaction)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "report_image_manager_open_failed",
                exc_info=error,
                guild_id=ctx.guild_id,
                **exception_fields(error),
            )
            await ctx.edit(embed=_error("No pude abrir los adjuntos ahora. Inténtalo de nuevo."))
            return
        if resolved is None:
            await ctx.edit(
                embed=_error("Usa este comando en el hilo de tu reporte o en uno que puedas supervisar.")
            )
            return
        submission, _, root = resolved
        try:
            view = ReportImageManagerView(self, submission, ctx.author.id)
            preview = view.render(root)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "report_image_manager_render_failed",
                exc_info=error,
                guild_id=ctx.guild_id,
                root_message_id=root.id,
                **exception_fields(error),
            )
            await ctx.edit(embed=_error("No pude mostrar los adjuntos ahora. Inténtalo de nuevo."))
            return
        view.message = await ctx.edit(
            embed=preview,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def apply(
        self,
        interaction: discord.Interaction,
        view: ReportImageManagerView,
        *,
        action: Action,
        category: ArrestReportAssetCategory,
        target_id: UUID | None,
        upload: discord.Attachment | None,
    ) -> tuple[ArrestReportSubmission, discord.Message]:
        lock = self._locks.setdefault(view.submission.id, asyncio.Lock())
        async with lock:
            resolved = await self.resolve(interaction)
            if (
                resolved is None
                or resolved[0].id != view.submission.id
                or interaction.user is None
                or interaction.user.id != view.requester_id
            ):
                raise ValueError("Ya no tienes acceso a este reporte desde este hilo.")
            submission, thread, root = resolved
            assets = ordered_assets(submission)
            if frozenset(asset.id for asset in assets) != view.asset_ids:
                raise ValueError("Los adjuntos cambiaron. Abre el gestor nuevamente.")
            generated_media = _generated_media(root)
            generated = _generated_attachment(root, submission, generated_media)
            bundle = await self.cog.provider.cached(submission.guild_id)
            generated_file: StructuredReportFile | None = None
            if generated_media is not None:
                if submission.sarp_arrest_id is None:
                    raise ValueError("El reporte no tiene un ID de PCU válido.")
                try:
                    # Components V2 can retain the gallery media when Message.attachments omits it.
                    payload = await (
                        generated.read() if generated is not None else generated_media.media.read()
                    )
                except discord.DiscordException as error:
                    raise ValueError(
                        "No pude recuperar la imagen del reporte. Inténtalo de nuevo más tarde."
                    ) from error
                if (
                    not payload.startswith(b"\x89PNG\r\n\x1a\n")
                    or len(payload) > bundle.settings.max_file_bytes
                ):
                    raise ValueError("La imagen del reporte no está disponible o supera el tamaño permitido.")
                generated_file = StructuredReportFile(
                    generated_report_filename(submission.sarp_arrest_id), "image/png", payload
                )
            pending: PendingArrestReportAsset | None = None
            if upload is not None:
                content_type = (upload.content_type or "").partition(";")[0].strip().casefold()
                if content_type not in ALLOWED_IMAGE_TYPES or upload.size <= 0:
                    raise ValueError("Adjunta una imagen JPG, PNG, WebP o GIF válida.")
                if upload.size > bundle.settings.max_file_bytes:
                    raise ValueError("La imagen supera el tamaño máximo permitido.")
                provisional = PendingArrestReportAsset(
                    category=category,
                    position=(
                        next((a.position for a in assets if a.id == target_id), 0)
                        if target_id is not None
                        else sum(a.category is category for a in assets)
                    ),
                    object_key="pending",
                    sha256="0" * 64,
                    filename=safe_filename(upload.filename),
                    content_type=content_type,
                    size_bytes=upload.size,
                )
                preview = changed_assets(
                    assets,
                    action=action,
                    category=category,
                    target_id=target_id,
                    replacement=provisional,
                )
                validate_counts(preview, generated_report=generated_media is not None)
                if sum(asset.size_bytes for asset in preview) + (
                    len(generated_file.payload) if generated_file else 0
                ) > (
                    bundle.settings.max_total_file_bytes
                ):
                    raise ValueError("Los adjuntos superan el tamaño total permitido.")
                payload = await upload.read()
                stored = (
                    await self.cog.service.asset_store.store(
                        (UploadedAsset(upload.filename, content_type, payload),),
                        namespace=f"arrest-reports/{submission.guild_id}/{submission.id}",
                        max_files=1,
                        max_file_bytes=bundle.settings.max_file_bytes,
                        max_total_bytes=bundle.settings.max_total_file_bytes,
                        allowed_content_types=ALLOWED_IMAGE_TYPES,
                    )
                )[0]
                pending = PendingArrestReportAsset(
                    category=category,
                    position=provisional.position,
                    object_key=stored.object_key,
                    sha256=stored.sha256,
                    filename=stored.filename,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                )
            candidate = changed_assets(
                assets, action=action, category=category, target_id=target_id, replacement=pending
            )
            validate_counts(candidate, generated_report=generated_media is not None)
            total_bytes = sum(asset.size_bytes for asset in candidate) + (
                len(generated_file.payload) if generated_file else 0
            )
            if total_bytes > bundle.settings.max_total_file_bytes:
                raise ValueError("Los adjuntos superan el tamaño total permitido.")
            card = _card_state(root)
            edited_root = await self._edit_root(root, submission, candidate, generated_file, card)
            try:
                updated = await self.cog.service.repository.sync_assets(
                    submission.id,
                    expected_ids=view.asset_ids,
                    assets=candidate,
                )
            except Exception:
                try:
                    await self._edit_root(root, submission, assets, generated_file, card)
                except Exception as rollback_error:
                    log_event(
                        LOGGER,
                        "error",
                        "report_image_rollback_failed",
                        exc_info=rollback_error,
                        guild_id=submission.guild_id,
                        thread_id=thread.id,
                        **exception_fields(rollback_error),
                    )
                raise
            assert interaction.user is not None
            notice = discord.Embed(
                title="🖼️ Adjuntos actualizados",
                description=f"<@{interaction.user.id}> {action} una {_category_name(category)}.",
                color=discord.Color.blue(),
            )
            try:
                await thread.send(embed=notice, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as error:
                log_event(
                    LOGGER,
                    "warning",
                    "report_image_notice_failed",
                    exc_info=error,
                    guild_id=submission.guild_id,
                    thread_id=thread.id,
                    **exception_fields(error),
                )
            log_event(
                LOGGER,
                "info",
                "report_image_updated",
                guild_id=submission.guild_id,
                thread_id=thread.id,
                actor_user_id=interaction.user.id,
                action=action,
                category=category.value,
                asset_count=len(updated.assets),
            )
            return updated, edited_root

    async def _edit_root(
        self,
        root: discord.Message,
        submission: ArrestReportSubmission,
        assets: tuple[AssetItem, ...],
        generated: StructuredReportFile | None,
        card: tuple[discord.Color | None, datetime | None],
    ) -> discord.Message:
        suspect_files: list[StructuredReportFile] = []
        supporting_files: list[StructuredReportFile] = []
        for asset in assets:
            file = StructuredReportFile(
                asset.filename, asset.content_type, await self.cog.service.storage.get(asset.object_key)
            )
            (
                suspect_files
                if asset.category is ArrestReportAssetCategory.SUSPECT_PHOTO
                else supporting_files
            ).append(file)
        files, suspect_names, supporting_names = build_discord_files(
            tuple(suspect_files),
            tuple(supporting_files),
            reserved_names=frozenset({generated.filename}) if generated else frozenset(),
        )
        output = list(files)
        if generated is not None:
            output.append(discord.File(io.BytesIO(generated.payload), filename=generated.filename))
        return await root.edit(
            attachments=[],
            files=output,
            view=build_submission_view(
                submission,
                suspect_filenames=suspect_names,
                supporting_filenames=supporting_names,
                report_filename=generated.filename if generated else None,
                report_refreshed_at=card[1],
                color=card[0],
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _generated_media(root: discord.Message) -> discord.MediaGalleryItem | None:
    card = discord.ui.DesignerView.from_message(root, timeout=None)
    container = next(item for item in card.children if isinstance(item, discord.ui.Container))
    for index, item in enumerate(container.items[:-1]):
        if isinstance(item, discord.ui.TextDisplay) and item.content == "### 📄 Reporte de arresto":
            gallery = container.items[index + 1]
            if isinstance(gallery, discord.ui.MediaGallery) and gallery.items:
                return gallery.items[0]
    return None


def _generated_attachment(
    root: discord.Message,
    submission: ArrestReportSubmission,
    media: discord.MediaGalleryItem | None = None,
) -> discord.Attachment | None:
    media = media if media is not None else _generated_media(root)
    if media is None or submission.sarp_arrest_id is None:
        return None
    attachment_id = media.media.attachment_id
    if attachment_id is not None:
        matching_id = next((item for item in root.attachments if item.id == attachment_id), None)
        if matching_id is not None:
            return matching_id
    filename = generated_report_filename(submission.sarp_arrest_id)
    return next((item for item in root.attachments if item.filename == filename), None)


def _card_state(root: discord.Message) -> tuple[discord.Color | None, datetime | None]:
    card = discord.ui.DesignerView.from_message(root, timeout=None)
    container = next(item for item in card.children if isinstance(item, discord.ui.Container))
    timestamp = next(
        (
            match.group(1)
            for item in container.items
            if isinstance(item, discord.ui.TextDisplay)
            if (match := re.search(r"Actualizado durante la última revisión · <t:(\d+):R>", item.content))
        ),
        None,
    )
    return container.color, datetime.fromtimestamp(int(timestamp), UTC) if timestamp else None


def _display_names(submission: ArrestReportSubmission, generated: bool) -> dict[UUID, str]:
    assets = ordered_assets(submission)
    used = (
        {generated_report_filename(submission.sarp_arrest_id).casefold()}
        if generated and submission.sarp_arrest_id is not None
        else set()
    )
    names: dict[UUID, str] = {}
    for index, asset in enumerate(assets, start=1):
        name = safe_filename(asset.filename)
        if name.casefold() in used:
            name = f"{index}-{name}"[:255]
        used.add(name.casefold())
        names[asset.id] = name
    return names


def _gallery_preview_url(
    root: discord.Message,
    submission: ArrestReportSubmission,
    selected: ArrestReportAsset,
) -> str | None:
    card = discord.ui.DesignerView.from_message(root, timeout=None)
    container = next(item for item in card.children if isinstance(item, discord.ui.Container))
    heading = (
        "### 📸 Fotografías del sospechoso"
        if selected.category is ArrestReportAssetCategory.SUSPECT_PHOTO
        else "### 📎 Respaldos adicionales"
    )
    for index, item in enumerate(container.items[:-1]):
        if not isinstance(item, discord.ui.TextDisplay):
            continue
        if item.content != heading and not (
            selected.category is ArrestReportAssetCategory.SUPPORTING_IMAGE
            and item.content == "### 📎 Reporte y respaldos"
        ):
            continue
        gallery = container.items[index + 1]
        if not isinstance(gallery, discord.ui.MediaGallery):
            return None
        category_assets = tuple(
            asset for asset in ordered_assets(submission) if asset.category is selected.category
        )
        position = next((i for i, asset in enumerate(category_assets) if asset.id == selected.id), None)
        if position is None or position >= len(gallery.items):
            return None
        url = gallery.items[position].url
        return url if url.startswith("https://") else None
    return None


class ImageSelect(discord.ui.Select["ReportImageManagerView"]):
    def __init__(self, submission: ArrestReportSubmission) -> None:
        super().__init__(
            placeholder="Selecciona una imagen para previsualizarla",
            options=_select_options(submission),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(ReportImageManagerView, self.view)
        view.selected_id = UUID(next(iter(self.values or ())))
        view.confirm_delete_id = None
        resolved = await view.manager.resolve(interaction)
        if resolved is None or resolved[0].id != view.submission.id:
            await interaction.response.send_message(
                embed=_error("Ya no tienes acceso a este reporte."), ephemeral=True
            )
            return
        view.submission = resolved[0]
        await interaction.response.edit_message(embed=view.render(resolved[2]), view=view)


def _select_options(submission: ArrestReportSubmission) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for asset in ordered_assets(submission):
        label = "Sospechoso" if asset.category is ArrestReportAssetCategory.SUSPECT_PHOTO else "Respaldo"
        options.append(
            discord.SelectOption(
                label=f"{label} {asset.position + 1}",
                value=str(asset.id),
                description=asset.filename[:100],
            )
        )
    return options


class ReportImageManagerView(discord.ui.View):
    def __init__(
        self, manager: ReportImageManager, submission: ArrestReportSubmission, requester_id: int
    ) -> None:
        super().__init__(timeout=300, disable_on_timeout=True)
        self.manager = manager
        self.submission = submission
        self.requester_id = requester_id
        self.asset_ids = frozenset(asset.id for asset in submission.assets)
        self.selected_id = ordered_assets(submission)[0].id
        self.confirm_delete_id: UUID | None = None
        self.message: discord.InteractionMessage | None = None
        self.add_item(ImageSelect(submission))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_error("Solo quien abrió este gestor puede usarlo."), ephemeral=True
        )
        return False

    def render(self, root: discord.Message) -> discord.Embed:
        assets = ordered_assets(self.submission)
        self.asset_ids = frozenset(asset.id for asset in assets)
        selected = next((asset for asset in assets if asset.id == self.selected_id), assets[0])
        self.selected_id = selected.id
        select = next(item for item in self.children if isinstance(item, ImageSelect))
        select.options = _select_options(self.submission)
        for option in select.options:
            option.default = option.value == str(selected.id)
        generated = _generated_media(root) is not None
        names = _display_names(self.submission, generated)
        attachment = next((item for item in root.attachments if item.filename == names[selected.id]), None)
        label = _category_name(selected.category).capitalize()
        embed = discord.Embed(
            title="🖼️ Imágenes del reporte",
            description=f"**{label} {selected.position + 1}** · {len(assets)} adjuntos disponibles",
            color=discord.Color.blurple(),
        )
        if attachment is not None:
            embed.set_image(url=attachment.url)
        elif (url := _gallery_preview_url(root, self.submission, selected)) is not None:
            embed.set_image(url=url)
        else:
            embed.add_field(name="Vista previa", value="No disponible. Abre el gestor nuevamente.")
        suspect_count = sum(a.category is ArrestReportAssetCategory.SUSPECT_PHOTO for a in assets)
        support_count = sum(
            a.category is ArrestReportAssetCategory.SUPPORTING_IMAGE for a in assets
        )
        self.add_suspect.disabled = suspect_count >= 2
        self.add_support.disabled = support_count >= (7 if generated else 8)
        self.remove.disabled = (
            selected.category is ArrestReportAssetCategory.SUSPECT_PHOTO and suspect_count == 1
        ) or (
            selected.category is ArrestReportAssetCategory.SUPPORTING_IMAGE
            and not generated
            and support_count == 1
        )
        self.remove.label = "Confirmar eliminación" if self.confirm_delete_id == selected.id else "Quitar"
        if self.remove.disabled:
            embed.set_footer(text="Esta imagen es obligatoria. Puedes reemplazarla, pero no quitarla.")
        elif self.confirm_delete_id == selected.id:
            embed.set_footer(text="Vuelve a pulsar «Confirmar eliminación» para quitar esta imagen.")
        return embed

    async def _open_upload(
        self, interaction: discord.Interaction, category: ArrestReportAssetCategory, action: Action
    ) -> None:
        resolved = await self.manager.resolve(interaction)
        if resolved is None or resolved[0].id != self.submission.id:
            await interaction.response.send_message(
                embed=_error("Ya no tienes acceso a este reporte."), ephemeral=True
            )
            return
        current_ids = frozenset(asset.id for asset in resolved[0].assets)
        if current_ids != self.asset_ids:
            await interaction.response.send_message(
                embed=_error("Los adjuntos cambiaron. Abre el gestor nuevamente."), ephemeral=True
            )
            return
        await interaction.response.send_modal(ImageUploadModal(self, category, action))

    @discord.ui.button(label="Añadir sospechoso", emoji="📸", style=discord.ButtonStyle.secondary, row=1)
    async def add_suspect(
        self, _button: discord.ui.Button[ReportImageManagerView], interaction: discord.Interaction
    ) -> None:
        await self._open_upload(interaction, ArrestReportAssetCategory.SUSPECT_PHOTO, "agregó")

    @discord.ui.button(label="Añadir respaldo", emoji="📎", style=discord.ButtonStyle.secondary, row=1)
    async def add_support(
        self, _button: discord.ui.Button[ReportImageManagerView], interaction: discord.Interaction
    ) -> None:
        await self._open_upload(interaction, ArrestReportAssetCategory.SUPPORTING_IMAGE, "agregó")

    @discord.ui.button(label="Reemplazar", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
    async def replace(
        self, _button: discord.ui.Button[ReportImageManagerView], interaction: discord.Interaction
    ) -> None:
        selected = next(asset for asset in self.submission.assets if asset.id == self.selected_id)
        await self._open_upload(interaction, selected.category, "reemplazó")

    @discord.ui.button(label="Quitar", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove(
        self, _button: discord.ui.Button[ReportImageManagerView], interaction: discord.Interaction
    ) -> None:
        resolved = await self.manager.resolve(interaction)
        if resolved is None or resolved[0].id != self.submission.id:
            await interaction.response.send_message(
                embed=_error("Ya no tienes acceso a este reporte."), ephemeral=True
            )
            return
        if frozenset(asset.id for asset in resolved[0].assets) != self.asset_ids:
            await interaction.response.send_message(
                embed=_error("Los adjuntos cambiaron. Abre el gestor nuevamente."), ephemeral=True
            )
            return
        if self.confirm_delete_id != self.selected_id:
            self.confirm_delete_id = self.selected_id
            await interaction.response.edit_message(embed=self.render(resolved[2]), view=self)
            return
        selected = next(asset for asset in self.submission.assets if asset.id == self.selected_id)
        await interaction.response.defer(invisible=True)
        try:
            updated, root = await self.manager.apply(
                interaction,
                self,
                action="eliminó",
                category=selected.category,
                target_id=selected.id,
                upload=None,
            )
        except (ValueError, AssetLimitError) as error:
            await interaction.followup.send(embed=_error(str(error)), ephemeral=True)
            return
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "report_image_delete_failed",
                exc_info=error,
                guild_id=interaction.guild_id,
                **exception_fields(error),
            )
            await interaction.followup.send(embed=_error("Inténtalo nuevamente más tarde."), ephemeral=True)
            return
        self.submission = updated
        self.confirm_delete_id = None
        await interaction.edit_original_response(embed=self.render(root), view=self)


class ImageUploadModal(discord.ui.DesignerModal):
    def __init__(
        self, view: ReportImageManagerView, category: ArrestReportAssetCategory, action: Action
    ) -> None:
        self.manager_view = view
        self.category = category
        self.action = action
        self.target_id = view.selected_id if action == "reemplazó" else None
        self.upload = discord.ui.FileUpload(min_values=1, max_values=1, required=True)
        super().__init__(
            discord.ui.Label("Imagen nueva", self.upload, description="JPG, PNG, WebP o GIF"),
            title=("Reemplazar" if action == "reemplazó" else "Añadir") + " imagen del reporte",
            timeout=300,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(invisible=True)
        attachment = next(iter(self.upload.values or ()), None)
        if not isinstance(attachment, discord.Attachment):
            await interaction.followup.send(embed=_error("Adjunta una imagen válida."), ephemeral=True)
            return
        try:
            updated, root = await self.manager_view.manager.apply(
                interaction,
                self.manager_view,
                action=self.action,
                category=self.category,
                target_id=self.target_id,
                upload=attachment,
            )
        except (ValueError, AssetLimitError) as error:
            await interaction.followup.send(embed=_error(str(error)), ephemeral=True)
            return
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "report_image_upload_failed",
                exc_info=error,
                guild_id=interaction.guild_id,
                **exception_fields(error),
            )
            await interaction.followup.send(embed=_error("Inténtalo nuevamente más tarde."), ephemeral=True)
            return
        previous_ids = self.manager_view.asset_ids
        self.manager_view.submission = updated
        self.manager_view.confirm_delete_id = None
        added = next((asset for asset in updated.assets if asset.id not in previous_ids), None)
        if added is not None:
            self.manager_view.selected_id = added.id
        await interaction.edit_original_response(embed=self.manager_view.render(root), view=self.manager_view)
