"""Compact live Discord messages; media stays behind attachment tools."""

from __future__ import annotations

import json

import discord

from arrest_report_agent.application.conversations import IncomingMessage


def incoming_message(message: discord.Message, *, authorized: bool) -> IncomingMessage:
    payload: dict[str, object] = {
        "channel_id": str(message.channel.id),
        "message_id": str(message.id),
        "author_id": str(message.author.id),
        "author": str(getattr(message.author, "display_name", message.author.id))[:100],
        "authorized_staff": authorized,
        "content": str(getattr(message, "content", ""))[:4000],
    }
    reference = getattr(message, "reference", None)
    if reference is not None and getattr(reference, "message_id", None) is not None:
        payload["reply_to_message_id"] = str(reference.message_id)
    attachments = getattr(message, "attachments", ())
    if attachments:
        payload["attachments"] = [
            {
                "id": str(attachment.id),
                "filename": attachment.filename[:150],
                "content_type": attachment.content_type,
                "size": attachment.size,
            }
            for attachment in attachments[:10]
        ]
    # Human-shared embeds may supply useful text, but never download media or
    # embed arbitrarily large provider payloads in the live queue.
    embeds = getattr(message, "embeds", ())
    if embeds:
        payload["embeds"] = [
            {"title": str(embed.title or "")[:200], "description": str(embed.description or "")[:1000]}
            for embed in embeds[:2]
        ]
    return IncomingMessage(
        id=message.id,
        author_id=message.author.id,
        text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
