from __future__ import annotations

from datetime import UTC, datetime

from arrest_report_agent.domain import (
    ContextState,
    ConversationMessage,
    DiscordAttachment,
    InteractionMode,
    ReviewCase,
)
from arrest_report_agent.infrastructure.discord.context import render_agent_input


def test_render_includes_every_selected_character_and_report_image() -> None:
    attachments = [
        DiscordAttachment(
            id=index,
            message_id=10,
            filename=filename,
            url=f"https://cdn.discord.test/{filename}",
            content_type=content_type,
            size=1000,
            width=width,
            height=height,
            is_current=True,
        )
        for index, (filename, content_type, width, height) in enumerate(
            [
                ("gallery1.jpg", "image/jpeg", 465, 985),
                ("gallery2.jpg", "image/jpeg", 454, 884),
                ("charges.png", "image/png", 515, 285),
                ("report.png", "image/png", 995, 455),
            ],
            start=1,
        )
    ]
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=10,
        trigger_message_id=10,
        requester_user_id=20,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[
            ConversationMessage(
                id=10,
                author_id=20,
                author_display="Deputy",
                author_is_bot=False,
                content="Terry Parker <@99>",
                created_at=datetime.now(UTC),
                attachment_ids=[1, 2, 3, 4],
                is_trigger=True,
            )
        ],
        attachments=attachments,
        context_state=ContextState.COMPLETE,
    )

    rendered = render_agent_input(case, "original")
    content = rendered[0]["content"]
    images = [part for part in content if part["type"] == "input_image"]

    assert len(images) == 4
    assert all(image["detail"] == "original" for image in images)
    assert {image["image_url"] for image in images} == {attachment.url for attachment in attachments}


def test_render_preserves_reply_and_edit_context() -> None:
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=3,
        trigger_message_id=5,
        requester_user_id=20,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[
            ConversationMessage(
                id=3,
                author_id=20,
                author_display="Deputy",
                author_is_bot=False,
                content="Reporte raíz",
                created_at=datetime.now(UTC),
            ),
            ConversationMessage(
                id=5,
                author_id=20,
                author_display="Deputy",
                author_is_bot=False,
                content="Corregido, sargento",
                created_at=datetime.now(UTC),
                edited=True,
                reply_to_message_id=4,
                reply_excerpt="Falta justificar el cargo",
                is_trigger=True,
            ),
        ],
        attachments=[],
    )

    text = render_agent_input(case, "original")[0]["content"][0]["text"]

    assert "EDITADO" in text
    assert "respuesta_a=4" in text
    assert "Falta justificar el cargo" in text

