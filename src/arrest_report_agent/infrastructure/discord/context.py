"""Bounded reconstruction of Discord report and thread context."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any

import discord

from arrest_report_agent.config import AppSettings
from arrest_report_agent.domain import (
    ContextState,
    ConversationMessage,
    DiscordAttachment,
    InteractionMode,
    ReviewCase,
)
from arrest_report_agent.domain.errors import ContextUnavailableError

REQUESTER_ROLE_LIMIT = 8
REQUESTER_ROLE_NAME_MAX_CHARS = 80
REQUESTER_ROLES_TOTAL_MAX_CHARS = 320


@dataclass(frozen=True)
class ReviewTarget:
    root: discord.Message
    trigger: discord.Message
    parent: discord.TextChannel
    thread: discord.Thread | None


class DiscordContextBuilder:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    async def resolve_target(self, message: discord.Message) -> ReviewTarget:
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
            parent = thread.parent
            if not isinstance(parent, discord.TextChannel):
                raise ContextUnavailableError("Configured thread has no text-channel parent")
            try:
                root = await parent.fetch_message(thread.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                raise ContextUnavailableError("Unable to fetch the thread starter message") from error
            return ReviewTarget(root=root, trigger=message, parent=parent, thread=thread)
        if not isinstance(message.channel, discord.TextChannel):
            raise ContextUnavailableError("Review message is not in a supported text channel")
        return ReviewTarget(
            root=message,
            trigger=message,
            parent=message.channel,
            thread=message.thread,
        )

    async def build_case(self, target: ReviewTarget) -> ReviewCase:
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

        ordered = sorted(raw_messages.values(), key=lambda item: (item.created_at, item.id))
        messages, text_truncated = self._convert_messages(ordered, target)
        if text_truncated:
            context_state = ContextState.TRUNCATED
            notices.append("El texto del hilo fue reducido al límite operativo.")
        attachments, attachment_notices = self._select_attachments(ordered, target)
        if attachment_notices:
            notices.extend(attachment_notices)
            context_state = ContextState.TRUNCATED
        subject = _case_subject(target.root, self.settings.identity.display_name)
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
            requester_role_names=_requester_role_names(requester),
            interaction_mode=(
                InteractionMode.THREAD_FOLLOW_UP
                if isinstance(target.trigger.channel, discord.Thread)
                else InteractionMode.INITIAL_REVIEW
            ),
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
            content = message.content.strip()
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
                    content=content,
                    created_at=message.created_at,
                    edited=message.edited_at is not None,
                    reply_to_message_id=reference_id,
                    reply_excerpt=reply_excerpt,
                    attachment_ids=[attachment.id for attachment in message.attachments],
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
        historical_messages = [
            item for item in reversed(ordered) if item.id not in {target.root.id, target.trigger.id}
        ]
        prioritized = trigger_messages + root_messages
        historical_images_used = 0
        selected: list[DiscordAttachment] = []
        seen_ids: set[int] = set()

        def add_from(message: discord.Message, historical: bool) -> None:
            nonlocal historical_images_used
            for attachment in message.attachments:
                if attachment.id in seen_ids:
                    continue
                content_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
                if not content_type or not content_type.startswith("image/"):
                    continue
                if attachment.size > self.settings.max_image_bytes:
                    notices.append(f"Se omitió un adjunto mayor al límite de tamaño ({attachment.id}).")
                    continue
                if (
                    historical
                    and historical_images_used
                    >= self.settings.thread_context_max_historical_images
                ):
                    notices.append("Se limitaron las imágenes históricas del hilo.")
                    return
                if len(selected) >= self.settings.max_images_per_review:
                    notices.append("Se alcanzó el máximo de imágenes por revisión.")
                    return
                seen_ids.add(attachment.id)
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
                    )
                )
                if historical:
                    historical_images_used += 1

        for message in prioritized:
            add_from(message, historical=False)
        for message in historical_messages:
            add_from(message, historical=True)
        return selected, list(dict.fromkeys(notices))


def render_agent_input(case: ReviewCase, image_detail: str) -> list[dict[str, Any]]:
    """Render compact text plus labeled images for one Agents SDK run."""

    transcript_lines = [
        "Atiende la solicitud actual dentro de tu ámbito usando el contexto disponible.",
        f"Modo: {case.interaction_mode.value}",
        f"Estado del contexto: {case.context_state.value}",
    ]
    if case.requester_display_name:
        transcript_lines.append(f"Solicitante: {case.requester_display_name}")
    if case.requester_role_names:
        transcript_lines.append(
            "Roles del solicitante: " + ", ".join(case.requester_role_names)
        )
    if case.context_notice:
        transcript_lines.append(f"Aviso de contexto: {case.context_notice}")
    transcript_lines.append("\nMensajes en orden cronológico:")
    for message in case.messages:
        flags = []
        if message.id == case.root_message_id:
            flags.append("REPORTE_RAIZ")
        if message.is_trigger:
            flags.append("SOLICITUD_ACTUAL")
        if message.author_is_bot:
            flags.append("BOT")
        if message.edited:
            flags.append("EDITADO")
        label = ",".join(flags) or "MENSAJE"
        transcript_lines.append(
            f"[{label} id={message.id} autor={message.author_display} autor_id={message.author_id}]"
        )
        if message.reply_to_message_id:
            reply = f"respuesta_a={message.reply_to_message_id}"
            if message.reply_excerpt:
                reply += f' extracto="{message.reply_excerpt}"'
            transcript_lines.append(reply)
        transcript_lines.append(message.content or "[sin texto]")
        if message.attachment_ids:
            transcript_lines.append(
                "adjuntos=" + ",".join(str(attachment_id) for attachment_id in message.attachment_ids)
            )

    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": "\n".join(transcript_lines)}
    ]
    for index, attachment in enumerate(case.attachments, start=1):
        dimensions = (
            f"{attachment.width}x{attachment.height}"
            if attachment.width and attachment.height
            else "desconocidas"
        )
        content.append(
            {
                "type": "input_text",
                "text": (
                    f"Imagen {index}/{len(case.attachments)}; adjunto_id={attachment.id}; "
                    f"mensaje_id={attachment.message_id}; archivo={attachment.filename}; "
                    f"dimensiones={dimensions}. Identifica su función por el contenido, no por el nombre."
                ),
            }
        )
        content.append(
            {"type": "input_image", "image_url": attachment.url, "detail": image_detail}
        )
    return [{"role": "user", "content": content}]


def _reply_excerpt(message: discord.Message, by_id: dict[int, discord.Message]) -> str | None:
    if message.reference is None or message.reference.message_id is None:
        return None
    resolved = message.reference.resolved
    referenced = (
        resolved
        if isinstance(resolved, discord.Message)
        else by_id.get(message.reference.message_id)
    )
    if referenced is None:
        return None
    excerpt = referenced.content.strip() or "[mensaje con adjuntos]"
    return excerpt[:180]


def _requester_role_names(author: discord.abc.User) -> list[str]:
    """Return a small hierarchy-ordered role summary without exposing role IDs."""

    candidates: list[tuple[int, int, str]] = []
    for role in getattr(author, "roles", ()):
        name = " ".join(str(getattr(role, "name", "")).split())
        is_default = getattr(role, "is_default", None)
        if (
            not name
            or name == "@everyone"
            or bool(getattr(role, "managed", False))
            or (callable(is_default) and is_default())
        ):
            continue
        candidates.append(
            (
                int(getattr(role, "position", 0)),
                int(getattr(role, "id", 0)),
                name[:REQUESTER_ROLE_NAME_MAX_CHARS],
            )
        )

    names: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for _, _, name in sorted(candidates, reverse=True):
        normalized = name.casefold()
        if normalized in seen:
            continue
        separator_chars = 2 if names else 0
        if total_chars + separator_chars + len(name) > REQUESTER_ROLES_TOTAL_MAX_CHARS:
            break
        seen.add(normalized)
        names.append(name)
        total_chars += separator_chars + len(name)
        if len(names) == REQUESTER_ROLE_LIMIT:
            break
    return names


def _case_subject(message: discord.Message, display_name: str) -> str | None:
    content = message.content.replace(display_name, "").strip()
    for mention in message.raw_mentions:
        content = content.replace(f"<@{mention}>", "").replace(f"<@!{mention}>", "")
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first_line[:80] or None
