"""Bounded reconstruction of Discord report and thread context."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import discord

from arrest_report_agent.config import AppSettings, BotPersona
from arrest_report_agent.config.logging import log_event
from arrest_report_agent.domain import (
    ContextState,
    ConversationMessage,
    DiscordAttachment,
    InteractionMode,
    ReviewCase,
)
from arrest_report_agent.domain.errors import ContextUnavailableError

from .attachments import AttachmentInputKind, classify_attachment
from .member_directory import compact_role_names
from .trigger import TriggerActivation

PARTICIPANT_REGISTRY_LIMIT = 10
LOGGER = logging.getLogger(__name__)


class DiscordResponseRoute(StrEnum):
    """Safe routing constraint derived before the runner starts."""

    AGENT_CHOICE = "agent_choice"
    INLINE_REPLY = "inline_reply"
    THREAD = "thread"


@dataclass(frozen=True)
class ReviewTarget:
    root: discord.Message
    trigger: discord.Message
    parent: discord.TextChannel
    thread: discord.Thread | None
    response_route: DiscordResponseRoute = DiscordResponseRoute.INLINE_REPLY


class DiscordContextBuilder:
    def __init__(
        self,
        settings: AppSettings,
        persona: BotPersona,
        bot: discord.Client | None = None,
    ) -> None:
        self.settings = settings
        self.bot = bot
        self.persona = persona

    async def resolve_target(
        self,
        message: discord.Message,
        activation: TriggerActivation,
    ) -> ReviewTarget:
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
            parent = thread.parent
            if not isinstance(parent, discord.TextChannel):
                raise ContextUnavailableError("Configured thread has no text-channel parent")
            root = await self._resolve_thread_root(parent, thread, message)
            return ReviewTarget(
                root=root,
                trigger=message,
                parent=parent,
                thread=thread,
                response_route=DiscordResponseRoute.THREAD,
            )
        if not isinstance(message.channel, discord.TextChannel):
            raise ContextUnavailableError("Review message is not in a supported text channel")
        return ReviewTarget(
            root=message,
            trigger=message,
            parent=message.channel,
            thread=message.thread,
            response_route=(
                DiscordResponseRoute.INLINE_REPLY
                if activation is TriggerActivation.BOT_REPLY
                else DiscordResponseRoute.AGENT_CHOICE
            ),
        )

    async def resolve_report_owner_target(
        self,
        message: discord.Message,
        activation: TriggerActivation,
    ) -> ReviewTarget:
        """Resolve an owner-only target without any orphan-thread fallback."""

        configured_channels = self.settings.report_channel_allowlist.ids
        if configured_channels is None:
            raise ContextUnavailableError("Report owner access requires explicit report channels")
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
            parent = thread.parent
            if not isinstance(parent, discord.TextChannel) or parent.id not in configured_channels:
                raise ContextUnavailableError("Thread parent is not an authorized report channel")
            try:
                root = await parent.fetch_message(thread.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                raise ContextUnavailableError("Unable to verify the report thread root") from error
            self._require_human_owner(root, message.author.id)
            return ReviewTarget(
                root=root,
                trigger=message,
                parent=parent,
                thread=thread,
                response_route=DiscordResponseRoute.THREAD,
            )

        if not isinstance(message.channel, discord.TextChannel):
            raise ContextUnavailableError("Owner bootstrap is not in a text channel")
        if message.channel.id not in configured_channels:
            raise ContextUnavailableError("Owner bootstrap is outside a report channel")
        if activation is not TriggerActivation.MENTION:
            raise ContextUnavailableError("Owner bootstrap requires a direct report reply and mention")
        reference = message.reference
        if reference is None or reference.message_id is None:
            raise ContextUnavailableError("Owner bootstrap does not reply to a report")
        referenced_channel_id = reference.channel_id or message.channel.id
        if referenced_channel_id != message.channel.id:
            raise ContextUnavailableError("Owner bootstrap references another channel")
        resolved = reference.resolved or reference.cached_message
        if isinstance(resolved, discord.Message):
            root = resolved
        else:
            try:
                root = await message.channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                raise ContextUnavailableError("Unable to verify the replied report") from error
        if root.id == message.id:
            raise ContextUnavailableError("Owner bootstrap cannot target itself")
        self._require_human_owner(root, message.author.id)
        return ReviewTarget(
            root=root,
            trigger=message,
            parent=message.channel,
            thread=root.thread,
            response_route=DiscordResponseRoute.THREAD,
        )

    @staticmethod
    def _require_human_owner(root: discord.Message, requester_user_id: int) -> None:
        if root.author.id != requester_user_id:
            raise ContextUnavailableError("Requester is not the report owner")
        if root.author.bot or root.webhook_id is not None:
            raise ContextUnavailableError("Report root was not published by a human owner")

    async def _resolve_thread_root(
        self,
        parent: discord.TextChannel,
        thread: discord.Thread,
        trigger: discord.Message,
    ) -> discord.Message:
        """Resolve an optional thread starter without rejecting orphaned threads."""

        try:
            return await parent.fetch_message(thread.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            root: discord.Message | None = None
            fallback_source = "trigger_message"
            try:
                root = await anext(
                    thread.history(limit=1, oldest_first=True),
                    None,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                root = None
            if root is not None:
                fallback_source = "oldest_thread_message"
            else:
                root = trigger
            log_event(
                LOGGER,
                "warning",
                "thread_root_fallback_used",
                guild_id=trigger.guild.id if trigger.guild is not None else None,
                parent_channel_id=parent.id,
                thread_id=thread.id,
                trigger_message_id=trigger.id,
                resolved_root_message_id=root.id,
                fallback_source=fallback_source,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return root

    async def build_case(
        self,
        target: ReviewTarget,
        *,
        include_channel_history: bool = True,
    ) -> ReviewCase:
        raw_messages: dict[int, discord.Message] = {target.root.id: target.root}
        context_state = ContextState.COMPLETE
        notices: list[str] = []
        if target.thread is not None:
            try:
                history = [
                    item
                    async for item in target.thread.history(
                        limit=self.settings.thread_context_max_messages,
                        oldest_first=False,
                    )
                ]
            except (discord.Forbidden, discord.HTTPException) as error:
                raise ContextUnavailableError("Unable to read thread history") from error
            for item in history:
                raw_messages[item.id] = item
            raw_messages[target.trigger.id] = target.trigger
            if len(history) >= self.settings.thread_context_max_messages:
                context_state = ContextState.TRUNCATED
                notices.append("El historial fue limitado a los mensajes más recientes.")
        elif include_channel_history and self.settings.channel_context_max_messages > 0:
            try:
                history = [
                    item
                    async for item in target.parent.history(
                        limit=self.settings.channel_context_max_messages,
                        before=target.trigger,
                        oldest_first=False,
                    )
                ]
            except (discord.Forbidden, discord.HTTPException) as error:
                raise ContextUnavailableError("Unable to read channel history") from error
            for item in history:
                raw_messages[item.id] = item
            raw_messages[target.trigger.id] = target.trigger
            if len(history) >= self.settings.channel_context_max_messages:
                context_state = ContextState.TRUNCATED
                notices.append("El contexto del canal fue limitado a los mensajes más recientes.")

        ordered = sorted(raw_messages.values(), key=lambda item: (item.created_at, item.id))
        messages, text_truncated = self._convert_messages(ordered, target)
        if text_truncated:
            context_state = ContextState.TRUNCATED
            notices.append("El texto del hilo fue reducido al límite operativo.")
        attachments, attachment_notices = self._select_attachments(ordered, target)
        if attachment_notices:
            notices.extend(attachment_notices)
            context_state = ContextState.TRUNCATED
        subject = _case_subject(target.root, self.persona.identity.display_name)
        requester = target.trigger.author
        return ReviewCase(
            guild_id=target.root.guild.id,
            parent_channel_id=target.parent.id,
            channel_id=target.trigger.channel.id,
            thread_id=target.thread.id if target.thread else None,
            root_message_id=target.root.id,
            trigger_message_id=target.trigger.id,
            requester_user_id=requester.id,
            requester_display_name=getattr(requester, "display_name", requester.name),
            requester_role_names=compact_role_names(requester),
            interaction_mode=(
                InteractionMode.THREAD_FOLLOW_UP
                if isinstance(target.trigger.channel, discord.Thread)
                else InteractionMode.INITIAL_REVIEW
            ),
            response_route=target.response_route.value,
            case_subject=subject,
            messages=messages,
            attachments=attachments,
            context_state=context_state,
            context_notice=" ".join(notices) or None,
        )

    def _convert_messages(
        self, ordered: list[discord.Message], target: ReviewTarget
    ) -> tuple[list[ConversationMessage], bool]:
        converted: list[ConversationMessage] = []
        by_id = {message.id: message for message in ordered}
        truncated = False
        for message in ordered:
            content = _message_context_text(message)
            is_prior_channel_message = target.thread is None and message.id not in {
                target.root.id,
                target.trigger.id,
            }
            per_message_limit = self.settings.thread_context_max_message_chars
            if len(content) > per_message_limit:
                content = content[: per_message_limit - 1] + "…"
                truncated = True
            reference_id = message.reference.message_id if message.reference else None
            reply_excerpt = _reply_excerpt(message, by_id)
            converted.append(
                ConversationMessage(
                    id=message.id,
                    author_id=message.author.id,
                    author_display=getattr(message.author, "display_name", message.author.name),
                    author_is_bot=message.author.bot,
                    author_is_assistant=_is_own_bot(message.author, self.bot),
                    author_role_names=([] if message.author.bot else compact_role_names(message.author)),
                    content=content,
                    created_at=message.created_at,
                    edited=message.edited_at is not None,
                    reply_to_message_id=reference_id,
                    reply_excerpt=reply_excerpt,
                    attachment_ids=(
                        []
                        if is_prior_channel_message
                        else [attachment.id for attachment in message.attachments]
                    ),
                    ambient_attachment_summary=(
                        _ambient_attachment_summary(message.attachments) if is_prior_channel_message else None
                    ),
                    is_trigger=message.id == target.trigger.id,
                )
            )
        max_chars = self.settings.thread_context_max_chars
        protected_ids = {target.root.id, target.trigger.id}
        while sum(len(item.content) for item in converted) > max_chars:
            removable_index = next(
                (index for index, item in enumerate(converted) if item.id not in protected_ids),
                None,
            )
            if removable_index is None:
                break
            converted.pop(removable_index)
            truncated = True
        total_chars = sum(len(item.content) for item in converted)
        if total_chars > max_chars:
            overflow = total_chars - max_chars
            for index, item in sorted(
                enumerate(converted), key=lambda pair: len(pair[1].content), reverse=True
            ):
                if overflow <= 0:
                    break
                removable = min(max(len(item.content) - 100, 0), overflow)
                if removable:
                    new_length = len(item.content) - removable
                    converted[index] = item.model_copy(
                        update={"content": item.content[: new_length - 1] + "…"}
                    )
                    overflow -= removable
                    truncated = True
        return converted, truncated

    def _select_attachments(
        self, ordered: list[discord.Message], target: ReviewTarget
    ) -> tuple[list[DiscordAttachment], list[str]]:
        notices: list[str] = []
        trigger_messages = [item for item in ordered if item.id == target.trigger.id]
        root_messages = [
            item for item in ordered if item.id == target.root.id and item.id != target.trigger.id
        ]
        historical_messages = (
            [item for item in reversed(ordered) if item.id not in {target.root.id, target.trigger.id}]
            if target.thread is not None
            else []
        )
        prioritized = trigger_messages + root_messages
        historical_images_used = 0
        historical_files_used = 0
        image_count = 0
        file_count = 0
        total_file_bytes = 0
        selected: list[DiscordAttachment] = []
        seen_ids: set[int] = set()

        def add_from(message: discord.Message, historical: bool) -> None:
            nonlocal historical_files_used, historical_images_used
            nonlocal image_count, file_count, total_file_bytes
            for attachment in message.attachments:
                if attachment.id in seen_ids:
                    continue
                seen_ids.add(attachment.id)
                input_kind, content_type = classify_attachment(
                    attachment.filename,
                    attachment.content_type,
                )
                if input_kind is None:
                    if not historical:
                        notices.append(f"Se omitió el archivo no compatible {attachment.filename!r}.")
                    continue

                if input_kind is AttachmentInputKind.IMAGE:
                    if attachment.size > self.settings.max_image_bytes:
                        notices.append(f"Se omitió una imagen mayor al límite de tamaño ({attachment.id}).")
                        continue
                    if (
                        historical
                        and historical_images_used >= self.settings.thread_context_max_historical_images
                    ):
                        notices.append("Se limitaron las imágenes históricas del hilo.")
                        continue
                    if image_count >= self.settings.max_images_per_review:
                        notices.append("Se alcanzó el máximo de imágenes por revisión.")
                        continue
                else:
                    if attachment.size > self.settings.max_file_bytes:
                        notices.append(f"Se omitió un archivo mayor al límite de tamaño ({attachment.id}).")
                        continue
                    if (
                        historical
                        and historical_files_used >= self.settings.thread_context_max_historical_files
                    ):
                        notices.append("Se limitaron los archivos históricos del hilo.")
                        continue
                    if file_count >= self.settings.max_files_per_review:
                        notices.append("Se alcanzó el máximo de archivos por revisión.")
                        continue
                    if total_file_bytes + attachment.size > self.settings.max_total_file_bytes:
                        notices.append("Los archivos alcanzaron el límite de tamaño total.")
                        continue

                selected.append(
                    DiscordAttachment(
                        id=attachment.id,
                        message_id=message.id,
                        filename=attachment.filename,
                        url=attachment.url,
                        content_type=content_type,
                        size=attachment.size,
                        width=attachment.width,
                        height=attachment.height,
                        is_current=message.id == target.trigger.id,
                        input_kind=input_kind.value,
                    )
                )
                if input_kind is AttachmentInputKind.IMAGE:
                    image_count += 1
                    if historical:
                        historical_images_used += 1
                else:
                    file_count += 1
                    total_file_bytes += attachment.size
                    if historical:
                        historical_files_used += 1

        for message in prioritized:
            add_from(message, historical=False)
        for message in historical_messages:
            add_from(message, historical=True)
        return selected, list(dict.fromkeys(notices))


def render_agent_input(case: ReviewCase, image_detail: str) -> list[dict[str, Any]]:
    """Render Discord history as native user/assistant turns for one SDK run."""

    attachments_by_message: dict[int, list[DiscordAttachment]] = {}
    for attachment in case.attachments:
        attachments_by_message.setdefault(attachment.message_id, []).append(attachment)

    known_message_ids = {message.id for message in case.messages}
    orphan_attachments = [
        attachment for attachment in case.attachments if attachment.message_id not in known_message_ids
    ]
    rendered: list[dict[str, Any]] = []
    orphan_rendered = False
    for message in case.messages:
        if message.is_trigger and orphan_attachments:
            rendered.append(_render_orphan_attachments(orphan_attachments, image_detail))
            orphan_rendered = True

        message_attachments = attachments_by_message.get(message.id, [])
        text = _render_discord_message(
            message,
            case,
            loaded_attachment_count=len(message_attachments),
        )
        if message.is_trigger:
            text = _render_operational_context(case) + "\n\n" + text
        if message.author_is_assistant:
            content: list[dict[str, Any]] = [{"type": "output_text", "text": text, "annotations": []}]
        else:
            content = [{"type": "input_text", "text": text}]
            _append_attachment_inputs(content, message_attachments, image_detail)
        rendered.append(
            {
                "role": "assistant" if message.author_is_assistant else "user",
                "content": content,
            }
        )
        if message.author_is_assistant and message_attachments:
            rendered.append(_render_assistant_attachment_context(message_attachments, image_detail))

    if orphan_attachments and not orphan_rendered:
        rendered.append(_render_orphan_attachments(orphan_attachments, image_detail))
    if not any(message.is_trigger for message in case.messages):
        fallback_content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": _render_operational_context(case) + "\n\n[SOLICITUD_ACTUAL]\n[sin texto]",
            }
        ]
        if not rendered:
            _append_attachment_inputs(fallback_content, case.attachments, image_detail)
        rendered.append({"role": "user", "content": fallback_content})
    return rendered


def _render_operational_context(case: ReviewCase) -> str:
    lines = [
        "[CONTEXTO_OPERATIVO]",
        "Atiende la solicitud actual dentro de tu ámbito usando el contexto disponible.",
        f"Modo: {case.interaction_mode.value}",
        _discord_environment(case),
        (
            f"Canales consultables: actual={case.channel_id}; padre={case.parent_channel_id}; "
            f"hilo={case.thread_id if case.thread_id is not None else 'none'}"
        ),
        f"Estado del contexto: {case.context_state.value}",
    ]
    if case.requester_display_name:
        lines.append(f"Solicitante: {case.requester_display_name}")
    if case.requester_role_names:
        lines.append("Roles del solicitante: " + ", ".join(case.requester_role_names))
    participant_registry = _participant_registry(case)
    if participant_registry:
        lines.append("Participantes consultables (user_id=nombre): " + participant_registry)
    participant_roles = _other_participant_roles(case)
    if participant_roles:
        lines.append("Otros participantes y roles: " + " | ".join(participant_roles))
    if case.context_notice:
        lines.append(f"Aviso de contexto: {case.context_notice}")
    if _has_prior_channel_context(case):
        lines.append(
            "Los mensajes CONTEXTO_PREVIO_CANAL son contexto ambiental: úsalos solo para "
            "resolver referencias de la solicitud actual; no mezcles conversaciones ni expedientes."
        )
    return "\n".join(lines)


def _render_discord_message(
    message: ConversationMessage,
    case: ReviewCase,
    *,
    loaded_attachment_count: int,
) -> str:
    flags: list[str] = []
    if message.id == case.root_message_id:
        flags.append("REPORTE_RAIZ")
    if case.interaction_mode is not InteractionMode.THREAD_FOLLOW_UP and message.id not in {
        case.root_message_id,
        case.trigger_message_id,
    }:
        flags.append("CONTEXTO_PREVIO_CANAL")
    if message.is_trigger:
        flags.append("SOLICITUD_ACTUAL")
    if message.author_is_assistant:
        flags.append("RESPUESTA_DEL_AGENTE")
    elif message.author_is_bot:
        flags.append("BOT_EXTERNO")
    if message.edited:
        flags.append("EDITADO")
    label = ",".join(flags) or "MENSAJE"
    lines = [f"[{label} mensaje={message.id} autor={message.author_display}]"]
    if message.reply_to_message_id:
        reply = f"respuesta_a={message.reply_to_message_id}"
        if message.reply_excerpt:
            reply += f' extracto="{message.reply_excerpt}"'
        lines.append(reply)
    lines.append(message.content or "[sin texto]")
    if message.attachment_ids:
        total_attachments = len(message.attachment_ids)
        lines.append(f"Adjuntos cargados en contexto: {loaded_attachment_count}/{total_attachments}")
        if loaded_attachment_count < total_attachments:
            lines.append(
                "Hay adjuntos omitidos: recupéralos con get_message_attachments usando este "
                "message_id antes de concluir que falta material."
            )
    if message.ambient_attachment_summary:
        lines.append(
            "Adjuntos del contexto ambiental (contenido y URL omitidos): "
            + message.ambient_attachment_summary
        )
    return "\n".join(lines)


def _has_prior_channel_context(case: ReviewCase) -> bool:
    return case.interaction_mode is not InteractionMode.THREAD_FOLLOW_UP and any(
        message.id not in {case.root_message_id, case.trigger_message_id} for message in case.messages
    )


def _ambient_attachment_summary(attachments: list[discord.Attachment]) -> str | None:
    if not attachments:
        return None

    image_names: list[str] = []
    file_names: list[str] = []
    for attachment in attachments:
        input_kind, _ = classify_attachment(
            attachment.filename,
            attachment.content_type,
        )
        filename = " ".join(attachment.filename.split())[:80] or "sin nombre"
        if input_kind is AttachmentInputKind.IMAGE:
            image_names.append(filename)
        else:
            file_names.append(filename)

    parts: list[str] = []
    if image_names:
        parts.append(_attachment_group_summary("imagen", "imágenes", image_names))
    if file_names:
        parts.append(_attachment_group_summary("archivo", "archivos", file_names))
    return "; ".join(parts)[:500]


def _attachment_group_summary(singular: str, plural: str, names: list[str]) -> str:
    label = singular if len(names) == 1 else plural
    visible_names = names[:5]
    suffix = f", +{len(names) - len(visible_names)}" if len(names) > len(visible_names) else ""
    return f"{len(names)} {label} ({', '.join(visible_names)}{suffix})"


def _append_attachment_inputs(
    content: list[dict[str, Any]],
    attachments: list[DiscordAttachment],
    image_detail: str,
) -> None:
    images = [attachment for attachment in attachments if attachment.is_image]
    files = [attachment for attachment in attachments if not attachment.is_image]
    image_position = 0
    file_position = 0
    for attachment in attachments:
        if attachment.is_image:
            image_position += 1
            dimensions = (
                f"{attachment.width}x{attachment.height}"
                if attachment.width and attachment.height
                else "desconocidas"
            )
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        f"Imagen adjunta {image_position}/{len(images)}; "
                        f"archivo={attachment.filename}; dimensiones={dimensions}. "
                        "Identifica su función por el contenido, no por el nombre."
                    ),
                }
            )
            content.append({"type": "input_image", "image_url": attachment.url, "detail": image_detail})
            continue

        file_position += 1
        content.append(
            {
                "type": "input_text",
                "text": (
                    f"Archivo adjunto {file_position}/{len(files)}; "
                    f"archivo={attachment.filename}; tipo={attachment.content_type or 'desconocido'}. "
                    "Usa su contenido como material del expediente."
                ),
            }
        )
        content.append(
            {
                "type": "input_file",
                "file_url": attachment.url,
            }
        )


def _render_orphan_attachments(
    attachments: list[DiscordAttachment],
    image_detail: str,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "[CONTEXTO_PREVIO]\nAdjuntos históricos cuyo texto fue omitido por los límites de contexto."
            ),
        }
    ]
    _append_attachment_inputs(content, attachments, image_detail)
    return {"role": "user", "content": content}


def _render_assistant_attachment_context(
    attachments: list[DiscordAttachment],
    image_detail: str,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": ("[CONTEXTO_PREVIO]\nAdjuntos publicados por el agente en su respuesta anterior."),
        }
    ]
    _append_attachment_inputs(content, attachments, image_detail)
    return {"role": "user", "content": content}


def _discord_environment(case: ReviewCase) -> str:
    if case.interaction_mode is InteractionMode.THREAD_FOLLOW_UP:
        return "Entorno de Discord: hilo; la respuesta se publicará en este mismo hilo."
    if case.response_route == DiscordResponseRoute.INLINE_REPLY.value:
        return "Entorno de Discord: canal; la respuesta se publicará como reply al mensaje actual."
    if case.response_route == DiscordResponseRoute.AGENT_CHOICE.value:
        return (
            "Entorno de Discord: canal; una revisión se publicará en hilo. Para conversar, "
            "elige reply o hilo según la extensión y continuidad esperada."
        )
    return "Entorno de Discord: canal; la respuesta se publicará en el hilo de la solicitud."


def _reply_excerpt(message: discord.Message, by_id: dict[int, discord.Message]) -> str | None:
    if message.reference is None or message.reference.message_id is None:
        return None
    resolved = message.reference.resolved
    referenced = (
        resolved if isinstance(resolved, discord.Message) else by_id.get(message.reference.message_id)
    )
    if referenced is None:
        return None
    excerpt = _message_context_text(referenced) or "[mensaje con adjuntos]"
    return excerpt[:180]


def _message_context_text(message: discord.Message) -> str:
    """Merge message text with compact bot embeds so thread context is complete."""

    parts = [message.content.strip()] if message.content.strip() else []
    if bool(getattr(message.author, "bot", False)):
        for embed in getattr(message, "embeds", ()):
            embed_lines: list[str] = []
            title = str(getattr(embed, "title", "") or "").strip()
            description = str(getattr(embed, "description", "") or "").strip()
            if title:
                embed_lines.append(f"Título: {title}")
            if description:
                embed_lines.append(description)
            for field in getattr(embed, "fields", ()):
                name = str(getattr(field, "name", "") or "").strip()
                value = str(getattr(field, "value", "") or "").strip()
                if name or value:
                    embed_lines.append(f"{name or 'Detalle'}: {value}")
            footer = getattr(embed, "footer", None)
            footer_text = str(getattr(footer, "text", "") or "").strip()
            if footer_text:
                embed_lines.append(f"Pie: {footer_text}")
            if embed_lines:
                parts.append("[EMBED]\n" + "\n".join(embed_lines))
    return "\n\n".join(parts)


def _participant_registry(case: ReviewCase) -> str:
    """Expose each visible participant ID once so member tools can resolve it."""

    participants: list[str] = []
    seen_ids: set[int] = set()

    def add(user_id: int, display_name: str | None) -> None:
        if user_id in seen_ids or not display_name or len(participants) >= PARTICIPANT_REGISTRY_LIMIT:
            return
        seen_ids.add(user_id)
        compact_name = " ".join(display_name.split())[:100]
        if compact_name:
            participants.append(f"{user_id}={compact_name}")

    add(case.requester_user_id, case.requester_display_name)
    for message in reversed(case.messages):
        if message.author_is_assistant:
            continue
        add(message.author_id, message.author_display)
    return " | ".join(participants)


def _other_participant_roles(case: ReviewCase) -> list[str]:
    """Render each historical human participant once, excluding the active requester."""

    participants: list[str] = []
    seen_author_ids: set[int] = set()
    for message in case.messages:
        if (
            message.author_id == case.requester_user_id
            or message.author_id in seen_author_ids
            or message.author_is_bot
            or not message.author_role_names
        ):
            continue
        seen_author_ids.add(message.author_id)
        participants.append(f"{message.author_display}: {', '.join(message.author_role_names)}")
    return participants


def _is_own_bot(author: discord.abc.User, bot: discord.Client | None) -> bool:
    bot_user = bot.user if bot is not None else None
    return bot_user is not None and author.id == bot_user.id


def _case_subject(message: discord.Message, display_name: str) -> str | None:
    content = message.content.replace(display_name, "").strip()
    for mention in message.raw_mentions:
        content = content.replace(f"<@{mention}>", "").replace(f"<@!{mention}>", "")
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first_line[:80] or None
