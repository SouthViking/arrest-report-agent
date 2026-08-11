"""Owner-gated inline commands for versioned PostgreSQL configuration."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shlex
from time import monotonic

import discord

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
CONFIG_COMMAND_PREFIX = "$rac"
CONFIG_UNAUTHORIZED_COOLDOWN_SECONDS = 30.0
MAX_PERSONA_JSON_BYTES = 1_000_000
MAX_PERSONA_AVATAR_BYTES = 10_485_760


class InlineConfigAuthorizationError(PermissionError):
    """Raised when a valid inline command requires owner-only access."""


def parse_inline_config_command(content: str) -> tuple[str, ...] | None:
    """Return shell-like arguments only when ``$rac`` is the first exact token."""

    normalized = content.lstrip()
    prefix_end = len(CONFIG_COMMAND_PREFIX)
    if normalized[:prefix_end].lower() != CONFIG_COMMAND_PREFIX:
        return None
    if len(normalized) > prefix_end and not normalized[prefix_end].isspace():
        return None
    remainder = normalized[prefix_end:].strip()
    if not remainder:
        return ("ayuda",)
    return tuple(shlex.split(remainder))


def build_config_unauthorized_embed(persona: BotPersona, message_id: int) -> discord.Embed:
    """Build a stable, personality-aware denial from the persona's existing pool."""

    messages = persona.messages.access
    intro = messages.unauthorized_user_intros[message_id % len(messages.unauthorized_user_intros)]
    description = messages.unauthorized_user.format(
        form_of_address=persona.identity.default_form_of_address,
        intro=intro,
    )
    return discord.Embed(
        title="🔒 Configuración restringida",
        description=description,
        color=discord.Color.red(),
    )


class ConfigurationInlineController:
    """Dispatch ``$rac`` before mention handling without registering slash commands."""

    def __init__(
        self,
        settings: AppSettings,
        repository: ConfigurationRepository,
        provider: DatabaseConfigProvider,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.owner_ids = frozenset(settings.config_owner_user_ids)
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._unauthorized_cooldowns: dict[tuple[int, int], float] = {}

    async def handle(self, message: discord.Message, persona: BotPersona) -> bool:
        """Handle one inline command and report whether the message was consumed."""

        try:
            arguments = parse_inline_config_command(message.content)
        except ValueError as error:
            arguments = ()
            parse_error: Exception | None = error
        else:
            parse_error = None
        if arguments is None:
            return False
        if getattr(message.author, "bot", False):
            return True
        guild = message.guild
        if guild is None:
            return True
        try:
            authorized = await self._authorized(message)
        except Exception as error:
            await self._failure(message, error, operation="authorize")
            return True
        if not authorized:
            await self._send_unauthorized(message, persona)
            return True
        if parse_error is not None:
            await self._failure(message, parse_error, operation="parse")
            return True

        command = arguments[0].lower() if arguments else "invalid"
        log_event(
            LOGGER,
            "info",
            "inline_config_command_received",
            command=command,
            guild_id=guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            user_id=message.author.id,
        )
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            try:
                await self._dispatch(message, arguments)
            except InlineConfigAuthorizationError:
                await self._send_unauthorized(message, persona, apply_cooldown=False)
            except Exception as error:
                await self._failure(message, error, operation=command)
            else:
                log_event(
                    LOGGER,
                    "info",
                    "inline_config_command_completed",
                    command=command,
                    guild_id=guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    user_id=message.author.id,
                )
        return True

    async def _authorized(self, message: discord.Message) -> bool:
        if message.author.id in self.owner_ids:
            return True
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        bundle = await self.provider.cached(message.guild.id)
        policy = bundle.snapshot.policy(AccessPolicyKey.CONFIG_ADMIN_ROLES)
        return policy.mode is AccessPolicyMode.RESTRICTED and any(
            role.id in policy.ids for role in message.author.roles
        )

    async def _send_unauthorized(
        self,
        message: discord.Message,
        persona: BotPersona,
        *,
        apply_cooldown: bool = True,
    ) -> None:
        if message.guild is None:
            return
        key = (message.guild.id, message.author.id)
        now = monotonic()
        if apply_cooldown:
            previous = self._unauthorized_cooldowns.get(key)
            if previous is not None and now - previous < CONFIG_UNAUTHORIZED_COOLDOWN_SECONDS:
                log_event(
                    LOGGER,
                    "info",
                    "inline_config_unauthorized_suppressed",
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    user_id=message.author.id,
                )
                return
            self._unauthorized_cooldowns[key] = now
        try:
            await message.reply(
                embed=build_config_unauthorized_embed(persona, message.id),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "warning",
                "inline_config_unauthorized",
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=message.author.id,
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            log_event(
                LOGGER,
                "error",
                "inline_config_unauthorized_reply_failed",
                exc_info=error,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=message.author.id,
                **exception_fields(error),
            )

    async def _reply(
        self,
        message: discord.Message,
        title: str,
        description: str,
        *,
        color: discord.Color | None = None,
    ) -> None:
        await message.reply(
            embed=discord.Embed(
                title=title,
                description=description,
                color=color or discord.Color.blurple(),
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _failure(
        self,
        message: discord.Message,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        log_event(
            LOGGER,
            "error",
            "inline_config_command_failed",
            exc_info=error,
            operation=operation,
            guild_id=message.guild.id if message.guild else None,
            channel_id=message.channel.id,
            user_id=message.author.id,
            message_id=message.id,
            **exception_fields(error),
        )
        if isinstance(error, (LookupError, ValueError, json.JSONDecodeError)):
            description = str(error) or "No pude aplicar ese cambio."
        else:
            description = f"No pude aplicar ese cambio. Referencia: `{message.id}`."
        try:
            await self._reply(
                message,
                "⚠️ Configuración",
                description,
                color=discord.Color.red(),
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _dispatch(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        command = arguments[0].lower()
        tail = arguments[1:]
        if command in {"ayuda", "help"}:
            _expect_count(tail, 0, "$rac ayuda")
            await self._show_help(message)
        elif command == "ver":
            _expect_count(tail, 0, "$rac ver")
            await self._show_configuration(message)
        elif command == "borrador":
            await self._draft(message, tail)
        elif command == "acceso":
            await self._access(message, tail)
        elif command == "agente":
            await self._agent(message, tail)
        elif command == "modulo":
            await self._feature(message, tail)
        elif command == "persona":
            await self._persona(message, tail)
        elif command == "publicar":
            _expect_count(tail, 0, "$rac publicar")
            await self._publish(message)
        elif command == "rollback":
            _expect_count(tail, 1, "$rac rollback <revision>")
            await self._rollback(message, int(tail[0]))
        elif command == "perfil-global":
            await self._global_profile(message, tail)
        else:
            raise ValueError(f"Comando desconocido. Usa `{CONFIG_COMMAND_PREFIX} ayuda`.")

    async def _show_help(self, message: discord.Message) -> None:
        await self._reply(
            message,
            "🛠️ Administración de configuración",
            (
                "`$rac ver`\n"
                "`$rac borrador crear|descartar`\n"
                "`$rac acceso modo|agregar|quitar …`\n"
                "`$rac agente clave=valor …`\n"
                "`$rac modulo <clave> <on|off> ['{…}']`\n"
                "`$rac persona importar|publicar|asignar|revertir …`\n"
                "`$rac publicar` · `$rac rollback <revisión>`\n"
                "`$rac perfil-global <persona> [revisión]`"
            ),
        )

    async def _show_configuration(self, message: discord.Message) -> None:
        guild = _require_guild(message)
        snapshot = (await self.provider.cached(guild.id)).snapshot
        draft_id = await self.repository.draft_revision_id(guild.id)
        policies = "\n".join(
            f"• `{key.value}`: **{policy.mode.value}** ({len(policy.ids)})"
            for key, policy in sorted(snapshot.policies.items(), key=lambda item: item[0].value)
        )
        await self._reply(
            message,
            "⚙️ Configuración activa",
            (
                f"**Revisión:** {snapshot.revision}\n"
                f"**Persona:** `{snapshot.persona_id}` r{snapshot.persona_revision}\n"
                f"**Modelo:** `{snapshot.agent.model}` · `{snapshot.agent.reasoning_effort}`\n"
                f"**Borrador:** `{draft_id or 'ninguno'}`\n\n{policies}"
            ),
        )

    async def _draft(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        _expect_count(arguments, 1, "$rac borrador crear|descartar")
        guild = _require_guild(message)
        action = arguments[0].lower()
        if action == "crear":
            revision_id = await self.repository.create_draft(
                guild.id,
                actor_user_id=message.author.id,
                interaction_id=message.id,
            )
            await self._reply(message, "📝 Borrador creado", f"ID: `{revision_id}`")
        elif action == "descartar":
            await self.repository.discard_draft(
                guild.id,
                actor_user_id=message.author.id,
                interaction_id=message.id,
            )
            await self._reply(message, "🗑️ Borrador descartado", "La revisión activa no cambió.")
        else:
            raise ValueError("Usa `$rac borrador crear` o `$rac borrador descartar`.")

    async def _draft_policy(
        self,
        guild_id: int,
        raw_key: str,
    ) -> AccessPolicySnapshot:
        key = AccessPolicyKey(raw_key.strip().lower())
        draft_id = await self.repository.draft_revision_id(guild_id)
        if draft_id is None:
            raise LookupError("Crea un borrador antes de editar accesos.")
        snapshot = await self.repository.snapshot_for_revision(guild_id, draft_id)
        return snapshot.policy(key)

    async def _access(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        if not arguments:
            raise ValueError("Usa `$rac acceso modo|agregar|quitar …`.")
        action = arguments[0].lower()
        guild = _require_guild(message)
        if action == "modo":
            _expect_count(arguments, 3, "$rac acceso modo <política> <open|restricted|disabled>")
            current = await self._draft_policy(guild.id, arguments[1])
            mode = AccessPolicyMode(arguments[2].lower())
            ids = current.ids if mode is AccessPolicyMode.RESTRICTED else ()
            if mode is AccessPolicyMode.RESTRICTED and not ids:
                raise ValueError("Agrega al menos un ID antes de usar `restricted`.")
            updated = AccessPolicySnapshot(
                key=current.key,
                subject_type=current.subject_type,
                mode=mode,
                ids=ids,
            )
        elif action in {"agregar", "quitar"}:
            _expect_count(arguments, 3, f"$rac acceso {action} <política> <ids_csv>")
            current = await self._draft_policy(guild.id, arguments[1])
            supplied = _parse_snowflakes(arguments[2])
            values = set(current.ids)
            values.difference_update(supplied) if action == "quitar" else values.update(supplied)
            updated = AccessPolicySnapshot(
                key=current.key,
                subject_type=POLICY_SUBJECT_TYPES[current.key],
                mode=AccessPolicyMode.RESTRICTED if values else AccessPolicyMode.DISABLED,
                ids=tuple(sorted(values)),
            )
        else:
            raise ValueError("Usa `$rac acceso modo|agregar|quitar …`.")
        await self.repository.update_draft_policy(
            guild.id,
            updated,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(
            message,
            "🔐 Acceso actualizado",
            f"`{updated.key.value}` → **{updated.mode.value}** ({len(updated.ids)} IDs).",
        )

    async def _agent(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        if not arguments:
            raise ValueError("Indica al menos un cambio como `reasoning=low`.")
        guild = _require_guild(message)
        draft_id = await self.repository.draft_revision_id(guild.id)
        if draft_id is None:
            raise LookupError("Crea un borrador antes de editar el agente.")
        current = (await self.repository.snapshot_for_revision(guild.id, draft_id)).agent
        assignments = _parse_assignments(arguments)
        aliases = {
            "modelo": "model",
            "model": "model",
            "reasoning": "reasoning_effort",
            "contexto_hilo": "thread_context_max_messages",
            "contexto_canal": "channel_context_max_messages",
            "max_turnos": "max_agent_turns",
        }
        unknown = set(assignments) - set(aliases)
        if unknown:
            raise ValueError("Parámetros desconocidos: " + ", ".join(sorted(unknown)))
        integer_fields = {"thread_context_max_messages", "channel_context_max_messages", "max_agent_turns"}
        payload = current.model_dump()
        for key, value in assignments.items():
            target = aliases[key]
            payload[target] = int(value) if target in integer_fields else value
        agent = GuildAgentConfig.model_validate(payload)
        await self.repository.update_draft_agent(
            guild.id,
            agent,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(message, "🤖 Agente actualizado", "Los cambios quedaron en el borrador.")

    async def _feature(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        if len(arguments) not in {2, 3}:
            raise ValueError("Uso: `$rac modulo <clave> <on|off> ['{…}']`.")
        guild = _require_guild(message)
        enabled = _parse_boolean(arguments[1])
        payload = json.loads(arguments[2]) if len(arguments) == 3 else {}
        if not isinstance(payload, dict):
            raise ValueError("Las opciones del módulo deben ser un objeto JSON.")
        feature = GuildFeatureSnapshot(
            key=arguments[0].strip().lower(),
            enabled=enabled,
            schema_version=1,
            payload=payload,
        )
        await self.repository.update_draft_feature(
            guild.id,
            feature,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(message, "🧩 Módulo actualizado", "El cambio quedó en el borrador.")

    async def _persona(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        if not arguments:
            raise ValueError("Usa `$rac persona importar|publicar|asignar|revertir …`.")
        action = arguments[0].lower()
        tail = arguments[1:]
        if action == "importar":
            _expect_count(tail, 0, "$rac persona importar (adjunta JSON y avatar)")
            await self._import_persona(message)
        elif action == "publicar":
            _expect_count(tail, 2, "$rac persona publicar <persona> <revision>")
            await self.repository.publish_persona_revision(
                tail[0],
                int(tail[1]),
                actor_user_id=message.author.id,
                interaction_id=message.id,
            )
            await self._reply(message, "🎭 Persona publicada", f"`{tail[0]}` r{tail[1]} está disponible.")
        elif action in {"asignar", "revertir"}:
            if len(tail) not in ({1, 2} if action == "asignar" else {2}):
                usage = (
                    "$rac persona asignar <persona> [revision]"
                    if action == "asignar"
                    else "$rac persona revertir <persona> <revision>"
                )
                raise ValueError(f"Uso: `{usage}`.")
            revision = int(tail[1]) if len(tail) == 2 else None
            guild = _require_guild(message)
            await self.repository.assign_draft_persona(
                guild.id,
                tail[0],
                revision,
                actor_user_id=message.author.id,
                interaction_id=message.id,
            )
            title = "🎭 Persona asignada" if action == "asignar" else "↩️ Persona revertida"
            await self._reply(message, title, "La revisión elegida quedó en el borrador.")
        else:
            raise ValueError("Usa `$rac persona importar|publicar|asignar|revertir …`.")

    async def _import_persona(self, message: discord.Message) -> None:
        profile_attachment = next(
            (
                attachment
                for attachment in message.attachments
                if attachment.filename.lower().endswith(".json")
            ),
            None,
        )
        avatar_attachment = next(
            (
                attachment
                for attachment in message.attachments
                if attachment is not profile_attachment
                and (
                    (attachment.content_type or "").lower() in {"image/png", "image/jpeg", "image/gif"}
                    or attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))
                )
            ),
            None,
        )
        if profile_attachment is None or avatar_attachment is None:
            raise ValueError("Adjunta un perfil JSON y un avatar PNG, JPEG o GIF.")
        profile_bytes = await profile_attachment.read()
        avatar_bytes = await avatar_attachment.read()
        if len(profile_bytes) > MAX_PERSONA_JSON_BYTES:
            raise ValueError("El JSON de persona es demasiado grande.")
        if not avatar_bytes or len(avatar_bytes) > MAX_PERSONA_AVATAR_BYTES:
            raise ValueError("El avatar debe pesar entre 1 byte y 10 MiB.")
        profile = BotPersona.model_validate_json(profile_bytes)
        content_type = avatar_attachment.content_type or mimetypes.guess_type(avatar_attachment.filename)[0]
        if content_type not in {"image/png", "image/jpeg", "image/gif"}:
            raise ValueError("El avatar debe ser PNG, JPEG o GIF.")
        record = await self.repository.import_persona_draft(
            profile,
            avatar_bytes,
            content_type=content_type,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(
            message,
            "🎭 Persona importada",
            f"`{record.slug}` r{record.revision} quedó como borrador.",
        )

    async def _publish(self, message: discord.Message) -> None:
        guild = _require_guild(message)
        revision_id = await self.repository.publish_draft(
            guild.id,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(message, "✅ Configuración publicada", f"Revisión: `{revision_id}`")

    async def _rollback(self, message: discord.Message, revision: int) -> None:
        guild = _require_guild(message)
        revision_id = await self.repository.rollback(
            guild.id,
            revision,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(message, "↩️ Configuración restaurada", f"Nueva revisión: `{revision_id}`")

    async def _global_profile(self, message: discord.Message, arguments: tuple[str, ...]) -> None:
        if message.author.id not in self.owner_ids:
            raise InlineConfigAuthorizationError
        if len(arguments) not in {1, 2}:
            raise ValueError("Uso: `$rac perfil-global <persona> [revision]`.")
        revision = int(arguments[1]) if len(arguments) == 2 else None
        revision_id = await self.repository.set_global_persona_by_slug(
            arguments[0],
            revision,
            actor_user_id=message.author.id,
            interaction_id=message.id,
        )
        await self._reply(
            message,
            "🌐 Perfil global programado",
            f"Persona: `{arguments[0]}` · revisión DB `{revision_id}`.",
        )


def _require_guild(message: discord.Message) -> discord.Guild:
    if message.guild is None:
        raise ValueError("Este comando solo funciona dentro de una guild configurada.")
    return message.guild


def _expect_count(arguments: tuple[str, ...], count: int, usage: str) -> None:
    if len(arguments) != count:
        raise ValueError(f"Uso: `{usage}`.")


def _parse_assignments(arguments: tuple[str, ...]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for argument in arguments:
        key, separator, value = argument.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ValueError("Usa parámetros `clave=valor` separados por espacios.")
        normalized = key.strip().lower()
        if normalized in assignments:
            raise ValueError(f"El parámetro `{normalized}` está repetido.")
        assignments[normalized] = value.strip()
    return assignments


def _parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "on", "si", "sí"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    raise ValueError("Usa `on` u `off` para indicar el estado del módulo.")


def _parse_snowflakes(raw_value: str) -> tuple[int, ...]:
    values: set[int] = set()
    for item in raw_value.split(","):
        normalized = item.strip().removeprefix("<@&").removeprefix("<@!").removeprefix("<@")
        normalized = normalized.removeprefix("<#").removesuffix(">")
        if not normalized:
            continue
        value = int(normalized)
        if value <= 0:
            raise ValueError("Los IDs deben ser snowflakes positivos de Discord.")
        values.add(value)
    if not values:
        raise ValueError("Proporciona al menos un ID de Discord.")
    return tuple(sorted(values))
