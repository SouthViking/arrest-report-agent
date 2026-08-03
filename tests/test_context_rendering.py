from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from arrest_report_agent.domain import (
    ContextState,
    ConversationMessage,
    DiscordAttachment,
    InteractionMode,
    ReviewCase,
)
from arrest_report_agent.infrastructure.discord.context import (
    DiscordContextBuilder,
    ReviewTarget,
    render_agent_input,
)


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
        requester_display_name="Teniente Charles Harrington",
        requester_role_names=["Teniente", "GNT"],
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
    text = content[0]["text"]

    assert len(images) == 4
    assert all(image["detail"] == "original" for image in images)
    assert {image["image_url"] for image in images} == {attachment.url for attachment in attachments}
    assert "Solicitante: Teniente Charles Harrington" in text
    assert "Roles del solicitante: Teniente, GNT" in text
    assert "Atiende la solicitud actual" in text
    assert "Revisa el estado actual" not in text


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


def test_context_trimming_preserves_root_and_current_request(settings) -> None:
    settings.thread_context_max_chars = 500
    settings.thread_context_max_message_chars = 300

    def message(message_id: int, content: str):
        author = SimpleNamespace(id=10, display_name="Deputy", name="Deputy", bot=False)
        return SimpleNamespace(
            id=message_id,
            content=content,
            author=author,
            created_at=datetime.fromtimestamp(message_id, tz=UTC),
            edited_at=None,
            reference=None,
            attachments=[],
        )

    root = message(1, "ROOT " + "a" * 280)
    old = message(2, "OLD " + "b" * 280)
    trigger = message(3, "TRIGGER " + "c" * 280)
    target = ReviewTarget(
        root=root,
        trigger=trigger,
        parent=SimpleNamespace(id=20),
        thread=SimpleNamespace(id=1),
    )
    builder = DiscordContextBuilder(settings)

    converted, truncated = builder._convert_messages([root, old, trigger], target)

    assert truncated
    assert {item.id for item in converted} == {1, 3}
    assert sum(len(item.content) for item in converted) <= 500


@pytest.mark.asyncio
async def test_context_includes_only_brief_relevant_requester_role_names(settings) -> None:
    def role(
        role_id: int,
        name: str,
        position: int,
        *,
        managed: bool = False,
        default: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=role_id,
            name=name,
            position=position,
            managed=managed,
            is_default=lambda: default,
        )

    roles = [role(1, "@everyone", 0, default=True)]
    roles.extend(role(index, f"Rol {index}", index) for index in range(2, 11))
    roles.append(role(99, "Integración", 99, managed=True))
    author = SimpleNamespace(
        id=20,
        name="Charles",
        display_name="Charles Harrington",
        bot=False,
        roles=roles,
    )
    message = SimpleNamespace(
        id=10,
        content="Reporte <@99>",
        author=author,
        created_at=datetime.now(UTC),
        edited_at=None,
        reference=None,
        attachments=[],
        raw_mentions=[99],
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        thread=None,
    )
    target = ReviewTarget(
        root=message,
        trigger=message,
        parent=SimpleNamespace(id=2),
        thread=None,
    )

    case = await DiscordContextBuilder(settings).build_case(target)

    assert case.requester_display_name == "Charles Harrington"
    assert case.requester_role_names == [
        "Rol 10",
        "Rol 9",
        "Rol 8",
        "Rol 7",
        "Rol 6",
        "Rol 5",
        "Rol 4",
        "Rol 3",
    ]
    rendered = render_agent_input(case, "original")[0]["content"][0]["text"]
    assert (
        "Roles del solicitante: Rol 10, Rol 9, Rol 8, Rol 7, Rol 6, Rol 5, Rol 4, Rol 3"
        in rendered
    )
    assert "Integración" not in rendered
    assert "@everyone" not in rendered
    assert "role_id" not in rendered
