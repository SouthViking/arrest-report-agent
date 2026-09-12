"""Discord commands for generic blacklist registration and lookup."""

from __future__ import annotations

import logging
from contextlib import suppress

import discord
from discord.ext import commands

from arrest_report_agent.config import (
    BlacklistOptions,
    BlacklistTypeOptions,
    PcuProfileKind,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.discord.forms import (
    ResultVisibility,
    build_visibility_select,
    selected_visibility,
)
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import BlacklistEntry, BlacklistEntryStatus, parse_lookup
from .repository import BlacklistRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
SEARCH_RESULT_LIMIT = 10


class BlacklistCog(commands.Cog):
    """Resolve registration types from configured channels and query them guild-wide."""

    blacklist = discord.SlashCommandGroup(
        "blacklist",
        "Registra y consulta blacklists de la facción",
    )

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        repository: BlacklistRepository,
    ) -> None:
        self.provider = provider
        self.repository = repository

    @blacklist.command(
        name="registrar",
        description="Registra una entrada en la blacklist asociada al canal",
    )
    async def register(self, ctx: discord.ApplicationContext) -> None:
        resolved = await self._authorize_context(ctx, channel_scoped=True)
        if resolved is None or ctx.guild is None or ctx.channel is None:
            return
        _options, type_definition = resolved
        if type_definition is None:
            return
        await ctx.send_modal(
            BlacklistRegistrationModal(
                self,
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                type_definition=type_definition,
            )
        )

    @blacklist.command(
        name="consultar",
        description="Consulta blacklists por ID de PCU, URL o nombre",
    )
    async def lookup(self, ctx: discord.ApplicationContext) -> None:
        resolved = await self._authorize_context(ctx, channel_scoped=False)
        if resolved is None:
            return
        await ctx.send_modal(BlacklistLookupModal(self, requester_id=ctx.author.id))

    @blacklist.command(
        name="editar",
        description="Edita una entrada del tipo de blacklist asociado al canal",
    )
    async def edit(self, ctx: discord.ApplicationContext) -> None:
        resolved = await self._authorize_context(
            ctx,
            channel_scoped=True,
            managers_only=True,
        )
        if resolved is None or ctx.guild is None or ctx.channel is None:
            return
        _options, type_definition = resolved
        if type_definition is None:
            return
        await ctx.send_modal(
            BlacklistEditModal(
                self,
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                type_definition=type_definition,
            )
        )

    @blacklist.command(
        name="eliminar",
        description="Elimina una entrada del tipo de blacklist asociado al canal",
    )
    async def delete(self, ctx: discord.ApplicationContext) -> None:
        resolved = await self._authorize_context(
            ctx,
            channel_scoped=True,
            managers_only=True,
        )
        if resolved is None or ctx.guild is None or ctx.channel is None:
            return
        _options, type_definition = resolved
        if type_definition is None:
            return
        await ctx.send_modal(
            BlacklistDeleteModal(
                self,
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                type_definition=type_definition,
            )
        )

    @blacklist.command(
        name="info",
        description="Explica cómo registrar y consultar blacklists",
    )
    async def info(self, ctx: discord.ApplicationContext) -> None:
        resolved = await self._authorize_context(
            ctx,
            channel_scoped=False,
            require_operational=False,
        )
        if resolved is None:
            return
        options, _type_definition = resolved
        await ctx.respond(
            embed=build_info_embed(options),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def publish_registration(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_key: str,
        raw_pcu_urls: str,
        raw_subject_name: str,
        raw_reason: str,
    ) -> None:
        user = interaction.user
        if (
            interaction.guild is None
            or interaction.channel is None
            or user is None
            or user.id != requester_id
            or interaction.guild.id != guild_id
            or interaction.channel.id != channel_id
        ):
            await interaction.response.send_message(
                embed=_alert("Este formulario ya no es válido."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        entry: BlacklistEntry | None = None
        try:
            type_definition = await self._authorize_interaction(
                interaction,
                requester_id=requester_id,
                guild_id=guild_id,
                channel_id=channel_id,
                type_key=type_key,
            )
            if type_definition is None:
                raise ValueError("Este canal ya no tiene un tipo de blacklist habilitado.")
            await interaction.response.defer(ephemeral=True)
            entry, created = await self.repository.reserve(
                guild_id=guild_id,
                channel_id=channel_id,
                interaction_id=interaction.id,
                author_user_id=user.id,
                type_definition=type_definition,
                raw_pcu_urls=raw_pcu_urls,
                raw_subject_name=raw_subject_name,
                raw_reason=raw_reason,
            )
            if not created and entry.message_url is not None:
                with suppress(discord.HTTPException):
                    await interaction.delete_original_response()
                return
            pending_embed = build_entry_embed(
                entry.model_copy(update={"status": BlacklistEntryStatus.ACTIVE})
            )
            message = await interaction.channel.send(
                embed=pending_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            entry = await self.repository.mark_active(entry.id, message.id)
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
            log_event(
                LOGGER,
                "info",
                "blacklist_entry_published",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user.id,
                type_key=type_key,
                reference=entry.reference,
                pcu_profile_ids=list(entry.pcu_profile_ids),
            )
        except ValueError as error:
            await self._interaction_error(interaction, str(error))
        except Exception as error:
            if entry is not None and entry.status is BlacklistEntryStatus.PUBLISHING:
                try:
                    await self.repository.mark_failed(entry.id)
                except Exception:
                    LOGGER.exception("Failed to mark blacklist publication as failed")
            await self._interaction_failure(interaction, error, operation="register.publish")

    async def publish_edit(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_key: str,
        raw_reference: str,
        raw_pcu_urls: str,
        raw_subject_name: str,
        raw_reason: str,
    ) -> None:
        try:
            type_definition = await self._authorize_interaction(
                interaction,
                requester_id=requester_id,
                guild_id=guild_id,
                channel_id=channel_id,
                type_key=type_key,
                managers_only=True,
            )
            if type_definition is None:
                raise ValueError("Este canal ya no tiene un tipo de blacklist habilitado.")
            await interaction.response.defer(ephemeral=True)
            entry = await self.repository.get_active_by_reference(
                guild_id=guild_id,
                type_key=type_key,
                raw_reference=raw_reference,
            )
            if entry is None:
                raise LookupError("No encontré una entrada activa con esa referencia para este tipo.")
            pcu_urls = raw_pcu_urls.strip()
            subject_name = raw_subject_name.strip()
            reason = raw_reason.strip()
            if not any((pcu_urls, subject_name, reason)):
                raise ValueError("Indica al menos un campo que quieras modificar.")
            message = await _fetch_entry_message(interaction.guild, entry)
            updated = await self.repository.update_active(
                entry_id=entry.id,
                guild_id=guild_id,
                type_definition=type_definition,
                actor_user_id=requester_id,
                raw_pcu_urls=pcu_urls or "\n".join(entry.pcu_urls),
                raw_subject_name=subject_name or entry.subject_name,
                raw_reason=reason or entry.reason,
            )
            await message.edit(
                embed=build_entry_embed(updated),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
            log_event(
                LOGGER,
                "info",
                "blacklist_entry_edited",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                type_key=type_key,
                reference=updated.reference,
            )
        except (LookupError, ValueError) as error:
            await self._interaction_error(interaction, str(error))
        except Exception as error:
            await self._interaction_failure(interaction, error, operation="edit.publish")

    async def publish_delete(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_key: str,
        raw_reference: str,
    ) -> None:
        try:
            await self._authorize_interaction(
                interaction,
                requester_id=requester_id,
                guild_id=guild_id,
                channel_id=channel_id,
                type_key=type_key,
                managers_only=True,
            )
            await interaction.response.defer(ephemeral=True)
            entry = await self.repository.get_active_by_reference(
                guild_id=guild_id,
                type_key=type_key,
                raw_reference=raw_reference,
            )
            if entry is None:
                raise LookupError("No encontré una entrada activa con esa referencia para este tipo.")
            try:
                message = await _fetch_entry_message(interaction.guild, entry)
                await message.delete()
            except discord.NotFound:
                pass
            revoked = await self.repository.revoke_active(
                entry_id=entry.id,
                guild_id=guild_id,
                type_key=type_key,
                actor_user_id=requester_id,
            )
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
            log_event(
                LOGGER,
                "info",
                "blacklist_entry_revoked",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                type_key=type_key,
                reference=revoked.reference,
            )
        except (LookupError, ValueError) as error:
            await self._interaction_error(interaction, str(error))
        except Exception as error:
            await self._interaction_failure(interaction, error, operation="delete.publish")

    async def publish_lookup(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        raw_query: str,
        visibility: ResultVisibility,
    ) -> None:
        user = interaction.user
        if (
            interaction.guild is None
            or interaction.channel is None
            or user is None
            or user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_alert("Esta consulta ya no es válida."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            await self._authorize_interaction(
                interaction,
                requester_id=requester_id,
                guild_id=interaction.guild.id,
                require_channel_type=False,
            )
            parse_lookup(raw_query)
            entries = await self.repository.search(
                guild_id=interaction.guild.id,
                raw_query=raw_query,
                limit=SEARCH_RESULT_LIMIT,
            )
            if not entries:
                await interaction.response.send_message(
                    embed=_alert("No encontré registros activos para esa búsqueda."),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            embed = (
                build_entry_embed(entries[0])
                if len(entries) == 1
                else build_lookup_results_embed(raw_query, entries)
            )
            private = visibility is ResultVisibility.PRIVATE
            await interaction.response.send_message(
                embed=embed,
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "blacklist_lookup_published",
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                user_id=user.id,
                visibility=visibility.value,
                result_count=len(entries),
            )
        except ValueError as error:
            await self._interaction_error(interaction, str(error))
        except Exception as error:
            await self._interaction_failure(interaction, error, operation="lookup.publish")

    async def _authorize_context(
        self,
        ctx: discord.ApplicationContext,
        *,
        channel_scoped: bool,
        managers_only: bool = False,
        require_operational: bool = True,
    ) -> tuple[BlacklistOptions, BlacklistTypeOptions | None] | None:
        if ctx.guild is None or ctx.channel is None:
            await ctx.respond(
                embed=_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            options = bundle.settings.blacklist_options
            if options is None:
                await ctx.respond(
                    embed=_alert("Las blacklists no están habilitadas para este servidor."),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None
            if require_operational and not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(
                    embed=_alert("Solo el personal operativo de la facción puede usar este comando."),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None
            if managers_only and not _member_has_any_role(
                ctx.author,
                options.manager_role_ids,
            ):
                await ctx.respond(
                    embed=_alert("Solo Executive Staff o Command Staff puede usar este comando."),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None
            type_definition = options.type_for_channel(ctx.channel.id) if channel_scoped else None
            if channel_scoped and type_definition is None:
                await ctx.respond(
                    embed=_alert("Este canal no está asociado a un tipo de blacklist habilitado."),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None
            return options, type_definition
        except Exception as error:
            await self._context_failure(ctx, error, operation="access.resolve")
            return None

    async def _authorize_interaction(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int | None = None,
        type_key: str | None = None,
        managers_only: bool = False,
        require_channel_type: bool = True,
    ) -> BlacklistTypeOptions | None:
        user = interaction.user
        if (
            interaction.guild is None
            or interaction.channel is None
            or user is None
            or user.id != requester_id
            or interaction.guild.id != guild_id
            or (channel_id is not None and interaction.channel.id != channel_id)
        ):
            raise ValueError("Este formulario ya no es válido.")
        bundle = await self.provider.cached(guild_id)
        options = bundle.settings.blacklist_options
        if options is None:
            raise ValueError("Las blacklists no están habilitadas para este servidor.")
        if not _member_has_any_role(user, bundle.settings.operational_staff_role_ids):
            raise ValueError("Solo el personal operativo de la facción puede usar este comando.")
        if managers_only and not _member_has_any_role(user, options.manager_role_ids):
            raise ValueError("Solo Executive Staff o Command Staff puede usar este comando.")
        if not require_channel_type:
            return None
        resolved_channel_id = channel_id if channel_id is not None else interaction.channel.id
        type_definition = options.type_for_channel(resolved_channel_id)
        if type_definition is None or (type_key is not None and type_definition.key != type_key):
            raise ValueError("La configuración de este canal cambió; abre nuevamente el comando.")
        return type_definition

    async def _context_failure(
        self,
        ctx: discord.ApplicationContext,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        _log_failure(error, operation=operation, guild_id=ctx.guild.id if ctx.guild else None)
        await ctx.respond(
            embed=_alert(f"No pude completar la operación. Referencia: `{ctx.interaction.id}`."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _interaction_failure(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        _log_failure(
            error,
            operation=operation,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        await self._interaction_error(
            interaction,
            f"No pude completar la operación. Referencia: `{interaction.id}`.",
        )

    @staticmethod
    async def _interaction_error(interaction: discord.Interaction, description: str) -> None:
        embed = _alert(description)
        allowed_mentions = discord.AllowedMentions.none()
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
                allowed_mentions=allowed_mentions,
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                allowed_mentions=allowed_mentions,
            )


class BlacklistRegistrationModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: BlacklistCog,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_definition: BlacklistTypeOptions,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.type_key = type_definition.key
        multi_profile = type_definition.max_profiles > 1
        segment = _profile_segment(type_definition.profile_kind)
        self.pcu_profiles = discord.ui.InputText(
            custom_id="blacklist:v1:register:pcu_profiles",
            style=(discord.InputTextStyle.long if multi_profile else discord.InputTextStyle.short),
            placeholder=_registration_profile_placeholder(type_definition),
            min_length=45,
            max_length=1_000 if multi_profile else 200,
        )
        self.subject_name = discord.ui.InputText(
            custom_id="blacklist:v1:register:subject",
            placeholder=(
                "Nombre Apellido (usuario)"
                if type_definition.profile_kind is PcuProfileKind.ACCOUNT
                else "Nombre Apellido"
            ),
            min_length=3,
            max_length=100,
        )
        self.reason = discord.ui.InputText(
            custom_id="blacklist:v1:register:reason",
            style=discord.InputTextStyle.long,
            placeholder="Explica de forma concreta el motivo de la blacklist.",
            min_length=10,
            max_length=1_000,
        )
        profile_label = (
            f"URLs de {segment} en PCU (una por línea)" if multi_profile else "URL de la cuenta en PCU"
        )
        subject_label = (
            "Personaje con el que la solicitó"
            if type_definition.subject_label == "Personaje"
            else type_definition.subject_label
        )
        super().__init__(
            discord.ui.Label(profile_label[:45], self.pcu_profiles),
            discord.ui.Label(subject_label[:45], self.subject_name),
            discord.ui.Label("Razón de la blacklist", self.reason),
            title=f"Registrar blacklist · {type_definition.display_name}"[:45],
            custom_id=f"blacklist:v1:register:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish_registration(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            type_key=self.type_key,
            raw_pcu_urls=self.pcu_profiles.value or "",
            raw_subject_name=self.subject_name.value or "",
            raw_reason=self.reason.value or "",
        )


class BlacklistLookupModal(discord.ui.DesignerModal):
    def __init__(self, cog: BlacklistCog, *, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.query = discord.ui.InputText(
            custom_id="blacklist:v1:lookup:query",
            placeholder="ID, URL de PCU o nombre de la persona/personaje",
            min_length=1,
            max_length=200,
        )
        self.visibility = build_visibility_select(
            "blacklist:v1:lookup:visibility",
            default=ResultVisibility.PUBLIC,
        )
        super().__init__(
            discord.ui.Label("Búsqueda", self.query),
            discord.ui.Label("Quién puede ver el resultado", self.visibility),
            title="Consultar blacklists",
            custom_id=f"blacklist:v1:lookup:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish_lookup(
            interaction,
            requester_id=self.requester_id,
            raw_query=self.query.value or "",
            visibility=selected_visibility(
                self.visibility,
                default=ResultVisibility.PUBLIC,
            ),
        )


class BlacklistEditModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: BlacklistCog,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_definition: BlacklistTypeOptions,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.type_key = type_definition.key
        self.reference = discord.ui.InputText(
            custom_id="blacklist:v1:edit:reference",
            placeholder=f"Ej.: {type_definition.reference_prefix}-00001",
            min_length=4,
            max_length=32,
        )
        multi_profile = type_definition.max_profiles > 1
        segment = _profile_segment(type_definition.profile_kind)
        self.pcu_profiles = discord.ui.InputText(
            custom_id="blacklist:v1:edit:pcu_profiles",
            style=(discord.InputTextStyle.long if multi_profile else discord.InputTextStyle.short),
            placeholder=(
                "Nuevas URLs, una por línea; vacío para conservar"
                if multi_profile
                else "Nueva URL de PCU; déjala vacía para conservarla"
            ),
            required=False,
            max_length=1_000 if multi_profile else 200,
        )
        self.subject_name = discord.ui.InputText(
            custom_id="blacklist:v1:edit:subject",
            placeholder="Nuevo nombre; déjalo vacío para conservarlo",
            required=False,
            max_length=100,
        )
        self.reason = discord.ui.InputText(
            custom_id="blacklist:v1:edit:reason",
            style=discord.InputTextStyle.long,
            placeholder="Nueva razón; déjala vacía para conservarla",
            required=False,
            max_length=1_000,
        )
        profile_label = (
            f"Nuevas URLs de {segment} en PCU" if multi_profile else "Nueva URL de la cuenta en PCU"
        )
        subject_label = (
            "Nuevo personaje"
            if type_definition.subject_label == "Personaje"
            else f"Nueva {type_definition.subject_label.casefold()}"
        )
        super().__init__(
            discord.ui.Label("Referencia", self.reference),
            discord.ui.Label(profile_label[:45], self.pcu_profiles),
            discord.ui.Label(subject_label[:45], self.subject_name),
            discord.ui.Label("Nueva razón", self.reason),
            title=f"Editar blacklist · {type_definition.display_name}"[:45],
            custom_id=f"blacklist:v1:edit:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish_edit(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            type_key=self.type_key,
            raw_reference=self.reference.value or "",
            raw_pcu_urls=self.pcu_profiles.value or "",
            raw_subject_name=self.subject_name.value or "",
            raw_reason=self.reason.value or "",
        )


class BlacklistDeleteModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: BlacklistCog,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        type_definition: BlacklistTypeOptions,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.type_key = type_definition.key
        self.reference = discord.ui.InputText(
            custom_id="blacklist:v1:delete:reference",
            placeholder=f"Ej.: {type_definition.reference_prefix}-00001",
            min_length=4,
            max_length=32,
        )
        self.confirmation = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="blacklist:v1:delete:confirmation",
            placeholder="Confirma la eliminación",
            options=[
                discord.SelectOption(
                    label="Eliminar el registro y su mensaje",
                    value="confirm",
                    emoji="🗑️",
                )
            ],
        )
        super().__init__(
            discord.ui.Label("Referencia", self.reference),
            discord.ui.Label("Confirmación", self.confirmation),
            title=f"Eliminar blacklist · {type_definition.display_name}"[:45],
            custom_id=f"blacklist:v1:delete:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if next(iter(self.confirmation.values or ()), None) != "confirm":
            await interaction.response.send_message(
                embed=_alert("Debes confirmar la eliminación."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.cog.publish_delete(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            type_key=self.type_key,
            raw_reference=self.reference.value or "",
        )


def build_entry_embed(entry: BlacklistEntry) -> discord.Embed:
    title_name = discord.utils.escape_markdown(entry.type_name)
    embed = discord.Embed(
        title=f"🚫 {entry.reference} · Blacklist de {title_name}",
        description="Registro activo de blacklist.",
        color=discord.Color.red(),
        timestamp=entry.created_at,
    )
    embed.add_field(
        name=_profile_field_name(entry),
        value=_profile_links(entry, compact=False),
        inline=len(entry.pcu_profile_ids) == 1,
    )
    embed.add_field(
        name=entry.subject_label,
        value=discord.utils.escape_markdown(entry.subject_name),
        inline=True,
    )
    embed.add_field(
        name="Razón de la blacklist",
        value=discord.utils.escape_markdown(entry.reason),
        inline=False,
    )
    embed.add_field(name="Estado", value="🔴 Activa", inline=True)
    embed.add_field(name="Registrado por", value=f"<@{entry.author_user_id}>", inline=True)
    if entry.edited_by_user_id is not None and entry.edited_at is not None:
        edited_timestamp = int(entry.edited_at.timestamp())
        embed.add_field(
            name="Última edición",
            value=f"<@{entry.edited_by_user_id}> · <t:{edited_timestamp}:R>",
            inline=False,
        )
    embed.set_footer(text=f"Blacklist {entry.type_name} · {entry.reference}")
    return embed


def build_info_embed(options: BlacklistOptions) -> discord.Embed:
    embed = discord.Embed(
        title="🚫 Sistema de blacklists",
        description=(
            "Los tipos se determinan por el canal de registro. Ningún usuario puede elegir o "
            "alterar el tipo manualmente."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Registrar",
        value=(
            "Usa `/blacklist registrar` dentro de un canal configurado. Debes pertenecer al "
            "personal operativo de la facción."
        ),
        inline=False,
    )
    embed.add_field(
        name="Consultar",
        value=(
            "Usa `/blacklist consultar` desde cualquier canal y busca por ID de PCU, URL o "
            "nombre. El resultado es público por defecto, con opción privada."
        ),
        inline=False,
    )
    embed.add_field(
        name="Administrar registros",
        value=(
            "`/blacklist editar` y `/blacklist eliminar` funcionan en el canal del tipo y están "
            "reservados a Executive Staff y Command Staff. Usa la referencia visible del registro."
        ),
        inline=False,
    )
    enabled_types = [item for item in options.types if item.enabled]
    type_lines = []
    for item in enabled_types:
        profile_name = "cuenta" if item.profile_kind is PcuProfileKind.ACCOUNT else "personaje"
        quantity = (
            f"hasta {item.max_profiles} perfiles de {profile_name}"
            if item.max_profiles > 1
            else f"1 perfil de {profile_name}"
        )
        type_lines.append(
            f"• **{discord.utils.escape_markdown(item.display_name)}** · "
            f"`{item.reference_prefix}` · {quantity}"
        )
    for index, chunk in enumerate(_field_chunks(type_lines)):
        embed.add_field(
            name="Tipos habilitados" if index == 0 else "Tipos habilitados (continuación)",
            value=chunk,
            inline=False,
        )
    embed.set_footer(text="Los errores y rechazos de permisos siempre son privados.")
    return embed


def build_lookup_results_embed(
    raw_query: str,
    entries: tuple[BlacklistEntry, ...],
) -> discord.Embed:
    embed = discord.Embed(
        title="🔎 Resultados de blacklists",
        description=(
            f"Coincidencias para **{discord.utils.escape_markdown(raw_query.strip())}**. "
            "Los resultados incluyen todos los tipos registrados."
        ),
        color=discord.Color.red(),
    )
    for entry in entries:
        link = f"[Abrir registro]({entry.message_url})" if entry.message_url is not None else "Registro"
        reason = discord.utils.escape_markdown(entry.reason)
        if len(reason) > 300:
            reason = reason[:297].rstrip() + "..."
        embed.add_field(
            name=f"🚫 {entry.reference} · {discord.utils.escape_markdown(entry.type_name)}",
            value=(
                f"**{entry.subject_label}:** "
                f"{discord.utils.escape_markdown(entry.subject_name)}\n"
                f"**PCU:** {_profile_links(entry, compact=True)}\n"
                f"**Razón:** {reason}\n{link}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{len(entries)} coincidencia(s) · máximo {SEARCH_RESULT_LIMIT}")
    return embed


def _profile_segment(kind: PcuProfileKind) -> str:
    return "cuentas" if kind is PcuProfileKind.ACCOUNT else "personajes"


def _registration_profile_placeholder(type_definition: BlacklistTypeOptions) -> str:
    segment = "accounts" if type_definition.profile_kind is PcuProfileKind.ACCOUNT else "characters"
    return f"https://ucp.sarp.es/app/ucp/{segment}/overview/1234"


def _profile_field_name(entry: BlacklistEntry) -> str:
    if entry.profile_kind is PcuProfileKind.ACCOUNT:
        return "Cuentas de PCU" if len(entry.pcu_profile_ids) > 1 else "Cuenta de PCU"
    return "Personajes de PCU" if len(entry.pcu_profile_ids) > 1 else "Cuenta de PCU"


def _profile_links(entry: BlacklistEntry, *, compact: bool) -> str:
    label = "Cuenta" if entry.profile_kind is PcuProfileKind.ACCOUNT else "Personaje"
    links = [
        f"[{label} {index} · ID {profile_id}]({url})"
        for index, (profile_id, url) in enumerate(
            zip(entry.pcu_profile_ids, entry.pcu_urls, strict=True),
            start=1,
        )
    ]
    if len(links) == 1:
        return f"[ID {entry.pcu_profile_id} · Abrir perfil]({entry.pcu_url})"
    return (" · " if compact else "\n").join(links)


def _alert(description: str) -> discord.Embed:
    return discord.Embed(
        title="⚠️ Blacklist",
        description=description,
        color=discord.Color.red(),
    )


def _member_has_any_role(member: object, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    roles = getattr(member, "roles", ())
    return bool(allowed and any(getattr(role, "id", None) in allowed for role in roles))


async def _fetch_entry_message(
    guild: discord.Guild | None,
    entry: BlacklistEntry,
) -> discord.Message:
    if guild is None or entry.message_id is None:
        raise LookupError("El mensaje asociado al registro ya no está disponible.")
    channel = guild.get_channel(entry.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise LookupError("El canal original del registro ya no está disponible.")
    return await channel.fetch_message(entry.message_id)


def _field_chunks(lines: list[str], limit: int = 1_024) -> tuple[str, ...]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        separator_length = 1 if current else 0
        if current and current_length + separator_length + len(line) > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
            separator_length = 0
        current.append(line)
        current_length += separator_length + len(line)
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks)


def _log_failure(error: Exception, *, operation: str, guild_id: int | None) -> None:
    log_event(
        LOGGER,
        "error",
        "blacklist_command_failed",
        exc_info=error,
        operation=operation,
        guild_id=guild_id,
        **exception_fields(error),
    )
