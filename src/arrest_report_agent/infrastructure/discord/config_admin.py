"""Private Discord commands for versioned PostgreSQL configuration."""

from __future__ import annotations

import json
import logging
import mimetypes

import discord
from discord.ext import commands

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    BotPersona,
    GuildAgentConfig,
    GuildFeatureSnapshot,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import (
    ConfigurationRepository,
    DatabaseConfigProvider,
)

LOGGER = logging.getLogger(__name__)
MAX_PERSONA_JSON_BYTES = 1_000_000
MAX_PERSONA_AVATAR_BYTES = 10_485_760


class ConfigurationAdminCog(commands.Cog):
    """Edit drafts and atomically publish guild configuration."""

    config = discord.SlashCommandGroup(
        "config",
        "Administra la configuración dinámica del bot",
    )
    drafts = config.create_subgroup("borrador", "Administra el borrador de la guild")
    access = config.create_subgroup("acceso", "Edita políticas de acceso del borrador")
    personas = config.create_subgroup("persona", "Administra personalidades versionadas")

    def __init__(
        self,
        settings: AppSettings,
        repository: ConfigurationRepository,
        provider: DatabaseConfigProvider,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.provider = provider
        self.owner_ids = frozenset(settings.config_owner_user_ids)

    async def _authorize(
        self,
        ctx: discord.ApplicationContext,
        *,
        owner_only: bool = False,
    ) -> bool:
        guild = ctx.guild
        member = ctx.author
        if member.id in self.owner_ids:
            return True
        if owner_only or guild is None or not isinstance(member, discord.Member):
            await self._error(ctx, "No tienes autorización para administrar esta configuración.")
            return False
        try:
            bundle = await self.provider.get(guild.id)
        except Exception:
            await self._error(ctx, "La configuración de este servidor no está disponible.")
            return False
        policy = bundle.snapshot.policy(AccessPolicyKey.CONFIG_ADMIN_ROLES)
        allowed = policy.mode is AccessPolicyMode.RESTRICTED and any(
            role.id in policy.ids for role in member.roles
        )
        if not allowed:
            await self._error(ctx, "No tienes autorización para administrar esta configuración.")
        return allowed

    @staticmethod
    async def _respond(
        ctx: discord.ApplicationContext,
        title: str,
        description: str,
        *,
        color: discord.Color | None = None,
    ) -> None:
        await ctx.respond(
            embed=discord.Embed(
                title=title,
                description=description,
                color=color or discord.Color.blurple(),
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _error(self, ctx: discord.ApplicationContext, message: str) -> None:
        await self._respond(ctx, "⚠️ Configuración", message, color=discord.Color.red())

    async def _mutation_failed(
        self,
        ctx: discord.ApplicationContext,
        error: Exception,
        operation: str,
    ) -> None:
        log_event(
            LOGGER,
            "error",
            "database_configuration_command_failed",
            exc_info=error,
            operation=operation,
            guild_id=ctx.guild.id if ctx.guild else None,
            user_id=ctx.author.id,
            interaction_id=ctx.interaction.id,
            **exception_fields(error),
        )
        if isinstance(error, (LookupError, ValueError)):
            message = str(error) or "No se pudo aplicar el cambio solicitado."
        else:
            message = "No se pudo aplicar el cambio solicitado. Revisa los logs con el ID de interacción."
        await self._error(ctx, message)

    @config.command(name="ver", description="Muestra la revisión activa y el borrador")
    async def show_configuration(self, ctx: discord.ApplicationContext) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            snapshot = (await self.provider.get(ctx.guild.id)).snapshot
            draft_id = await self.repository.draft_revision_id(ctx.guild.id)
            policies = "\n".join(
                f"• `{key.value}`: **{policy.mode.value}** ({len(policy.ids)})"
                for key, policy in snapshot.policies.items()
            )
            await self._respond(
                ctx,
                "⚙️ Configuración activa",
                (
                    f"**Revisión:** {snapshot.revision}\n"
                    f"**Persona:** `{snapshot.persona_id}` r{snapshot.persona_revision}\n"
                    f"**Modelo:** `{snapshot.agent.model}` · `{snapshot.agent.reasoning_effort}`\n"
                    f"**Borrador:** `{draft_id or 'ninguno'}`\n\n{policies}"
                ),
            )
        except Exception as error:
            await self._mutation_failed(ctx, error, "show")

    @drafts.command(name="crear", description="Clona la configuración activa en un borrador")
    async def create_draft(self, ctx: discord.ApplicationContext) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            revision_id = await self.repository.create_draft(
                ctx.guild.id,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "📝 Borrador creado", f"ID: `{revision_id}`")
        except Exception as error:
            await self._mutation_failed(ctx, error, "draft.create")

    @drafts.command(name="descartar", description="Elimina el borrador sin publicarlo")
    async def discard_draft(self, ctx: discord.ApplicationContext) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            await self.repository.discard_draft(
                ctx.guild.id,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "🗑️ Borrador descartado", "No se modificó la revisión activa.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "draft.discard")

    async def _draft_policy(
        self,
        guild_id: int,
        raw_key: str,
    ) -> tuple[object, AccessPolicySnapshot]:
        key = AccessPolicyKey(raw_key.strip().lower())
        draft_id = await self.repository.draft_revision_id(guild_id)
        if draft_id is None:
            raise LookupError("Crea un borrador antes de editar accesos")
        snapshot = await self.repository.snapshot_for_revision(guild_id, draft_id)
        return snapshot, snapshot.policy(key)

    @access.command(name="modo", description="Cambia el modo open, restricted o disabled")
    async def set_access_mode(
        self,
        ctx: discord.ApplicationContext,
        politica: str,
        modo: str,
    ) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            _snapshot, current = await self._draft_policy(ctx.guild.id, politica)
            mode = AccessPolicyMode(modo.strip().lower())
            ids = current.ids if mode is AccessPolicyMode.RESTRICTED else ()
            if mode is AccessPolicyMode.RESTRICTED and not ids:
                raise ValueError("Agrega al menos un ID antes de usar restricted")
            await self.repository.update_draft_policy(
                ctx.guild.id,
                AccessPolicySnapshot(
                    key=current.key,
                    subject_type=current.subject_type,
                    mode=mode,
                    ids=ids,
                ),
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "🔐 Acceso actualizado", f"`{current.key.value}` → **{mode.value}**")
        except Exception as error:
            await self._mutation_failed(ctx, error, "access.mode")

    async def _change_policy_members(
        self,
        ctx: discord.ApplicationContext,
        raw_key: str,
        raw_ids: str,
        *,
        remove: bool,
    ) -> None:
        if ctx.guild is None:
            return
        _snapshot, current = await self._draft_policy(ctx.guild.id, raw_key)
        supplied = _parse_snowflakes(raw_ids)
        values = set(current.ids)
        values.difference_update(supplied) if remove else values.update(supplied)
        mode = AccessPolicyMode.RESTRICTED if values else AccessPolicyMode.DISABLED
        await self.repository.update_draft_policy(
            ctx.guild.id,
            AccessPolicySnapshot(
                key=current.key,
                subject_type=POLICY_SUBJECT_TYPES[current.key],
                mode=mode,
                ids=tuple(sorted(values)),
            ),
            actor_user_id=ctx.author.id,
            interaction_id=ctx.interaction.id,
        )

    @access.command(name="agregar", description="Agrega IDs CSV a una política del borrador")
    async def add_access_members(
        self,
        ctx: discord.ApplicationContext,
        politica: str,
        ids: str,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            await self._change_policy_members(ctx, politica, ids, remove=False)
            await self._respond(ctx, "🔐 Acceso actualizado", "Los IDs fueron agregados al borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "access.add")

    @access.command(name="quitar", description="Quita IDs CSV de una política del borrador")
    async def remove_access_members(
        self,
        ctx: discord.ApplicationContext,
        politica: str,
        ids: str,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            await self._change_policy_members(ctx, politica, ids, remove=True)
            await self._respond(ctx, "🔐 Acceso actualizado", "Los IDs fueron retirados del borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "access.remove")

    @config.command(name="agente", description="Ajusta modelo y límites principales del borrador")
    async def update_agent(
        self,
        ctx: discord.ApplicationContext,
        modelo: str | None = None,
        reasoning: str | None = None,
        contexto_hilo: int | None = None,
        contexto_canal: int | None = None,
        max_turnos: int | None = None,
    ) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            draft_id = await self.repository.draft_revision_id(ctx.guild.id)
            if draft_id is None:
                raise LookupError("Crea un borrador antes de editar el agente")
            current = (await self.repository.snapshot_for_revision(ctx.guild.id, draft_id)).agent
            payload = current.model_dump()
            replacements = {
                "model": modelo,
                "reasoning_effort": reasoning,
                "thread_context_max_messages": contexto_hilo,
                "channel_context_max_messages": contexto_canal,
                "max_agent_turns": max_turnos,
            }
            payload.update({key: value for key, value in replacements.items() if value is not None})
            agent = GuildAgentConfig.model_validate(payload)
            await self.repository.update_draft_agent(
                ctx.guild.id,
                agent,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "🤖 Agente actualizado", "Los cambios quedaron en el borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "agent.update")

    @config.command(name="modulo", description="Configura un módulo extensible del borrador")
    async def update_feature(
        self,
        ctx: discord.ApplicationContext,
        clave: str,
        habilitado: bool,
        opciones_json: str = "{}",
    ) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            payload = json.loads(opciones_json)
            if not isinstance(payload, dict):
                raise ValueError("opciones_json debe ser un objeto JSON")
            feature = GuildFeatureSnapshot(
                key=clave.strip().lower(),
                enabled=habilitado,
                schema_version=1,
                payload=payload,
            )
            await self.repository.update_draft_feature(
                ctx.guild.id,
                feature,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "🧩 Módulo actualizado", "El cambio quedó en el borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "feature.update")

    @personas.command(name="importar", description="Importa un JSON y avatar como borrador")
    async def import_persona(
        self,
        ctx: discord.ApplicationContext,
        perfil: discord.Attachment,
        avatar: discord.Attachment,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            profile_bytes = await perfil.read()
            avatar_bytes = await avatar.read()
            if len(profile_bytes) > MAX_PERSONA_JSON_BYTES:
                raise ValueError("El JSON de persona es demasiado grande")
            if not avatar_bytes or len(avatar_bytes) > MAX_PERSONA_AVATAR_BYTES:
                raise ValueError("El avatar debe pesar entre 1 byte y 10 MiB")
            profile = BotPersona.model_validate_json(profile_bytes)
            content_type = avatar.content_type or mimetypes.guess_type(avatar.filename)[0]
            if content_type not in {"image/png", "image/jpeg", "image/gif"}:
                raise ValueError("El avatar debe ser PNG, JPEG o GIF")
            record = await self.repository.import_persona_draft(
                profile,
                avatar_bytes,
                content_type=content_type,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(
                ctx,
                "🎭 Persona importada",
                f"`{record.slug}` r{record.revision} quedó como borrador.",
            )
        except Exception as error:
            await self._mutation_failed(ctx, error, "persona.import")

    @personas.command(name="publicar", description="Publica una revisión de personalidad")
    async def publish_persona(
        self,
        ctx: discord.ApplicationContext,
        persona: str,
        revision: int,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            await self.repository.publish_persona_revision(
                persona,
                revision,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "🎭 Persona publicada", f"`{persona}` r{revision} está disponible.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "persona.publish")

    async def _assign_persona(
        self,
        ctx: discord.ApplicationContext,
        persona: str,
        revision: int | None,
    ) -> None:
        if ctx.guild is None:
            return
        await self.repository.assign_draft_persona(
            ctx.guild.id,
            persona,
            revision,
            actor_user_id=ctx.author.id,
            interaction_id=ctx.interaction.id,
        )

    @personas.command(name="asignar", description="Asigna una persona publicada al borrador")
    async def assign_persona(
        self,
        ctx: discord.ApplicationContext,
        persona: str,
        revision: int | None = None,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            await self._assign_persona(ctx, persona, revision)
            await self._respond(ctx, "🎭 Persona asignada", "La asignación quedó en el borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "persona.assign")

    @personas.command(name="revertir", description="Asigna una revisión anterior al borrador")
    async def revert_persona(
        self,
        ctx: discord.ApplicationContext,
        persona: str,
        revision: int,
    ) -> None:
        if not await self._authorize(ctx):
            return
        try:
            await self._assign_persona(ctx, persona, revision)
            await self._respond(ctx, "↩️ Persona revertida", "La revisión elegida quedó en el borrador.")
        except Exception as error:
            await self._mutation_failed(ctx, error, "persona.revert")

    @config.command(name="publicar", description="Publica el borrador de forma atómica")
    async def publish_configuration(self, ctx: discord.ApplicationContext) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            revision_id = await self.repository.publish_draft(
                ctx.guild.id,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "✅ Configuración publicada", f"Revisión: `{revision_id}`")
        except Exception as error:
            await self._mutation_failed(ctx, error, "config.publish")

    @config.command(name="rollback", description="Restaura una revisión anterior como una nueva")
    async def rollback_configuration(
        self,
        ctx: discord.ApplicationContext,
        revision: int,
    ) -> None:
        if not await self._authorize(ctx) or ctx.guild is None:
            return
        try:
            revision_id = await self.repository.rollback(
                ctx.guild.id,
                revision,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(ctx, "↩️ Configuración restaurada", f"Nueva revisión: `{revision_id}`")
        except Exception as error:
            await self._mutation_failed(ctx, error, "config.rollback")

    @config.command(name="perfil-global", description="Cambia la identidad global de Discord")
    async def update_global_profile(
        self,
        ctx: discord.ApplicationContext,
        persona: str,
        revision: int | None = None,
    ) -> None:
        if not await self._authorize(ctx, owner_only=True):
            return
        try:
            revision_id = await self.repository.set_global_persona_by_slug(
                persona,
                revision,
                actor_user_id=ctx.author.id,
                interaction_id=ctx.interaction.id,
            )
            await self._respond(
                ctx,
                "🌐 Perfil global programado",
                f"Persona: `{persona}` · revisión DB `{revision_id}`. Se sincronizará de forma segura.",
            )
        except Exception as error:
            await self._mutation_failed(ctx, error, "global_profile.update")


def _parse_snowflakes(raw_value: str) -> tuple[int, ...]:
    values: set[int] = set()
    for item in raw_value.split(","):
        normalized = item.strip().removeprefix("<@&").removeprefix("<@!").removeprefix("<@")
        normalized = normalized.removeprefix("<#").removesuffix(">")
        if not normalized:
            continue
        value = int(normalized)
        if value <= 0:
            raise ValueError("Los IDs deben ser snowflakes positivos de Discord")
        values.add(value)
    if not values:
        raise ValueError("Proporciona al menos un ID de Discord")
    return tuple(sorted(values))
