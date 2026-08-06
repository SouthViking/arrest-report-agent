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
    DiscordResponseRoute,
    ReviewTarget,
    render_agent_input,
)
from arrest_report_agent.infrastructure.discord.trigger import TriggerActivation


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
        response_route="agent_choice",
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
    assert "Participantes consultables (user_id=nombre): 20=Teniente Charles Harrington" in text
    assert "Canales consultables: actual=2; padre=2; hilo=none" in text
    assert "Atiende la solicitud actual" in text
    assert "una revisión se publicará en hilo" in text
    assert "elige reply o hilo" in text
    assert "Revisa el estado actual" not in text


def test_render_includes_roles_of_other_human_participants_once() -> None:
    now = datetime.now(UTC)
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=10,
        trigger_message_id=14,
        requester_user_id=20,
        requester_display_name="Deputy Torres",
        requester_role_names=["Deputy Sheriff"],
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[
            ConversationMessage(
                id=10,
                author_id=30,
                author_display="Sargento Gómez",
                author_is_bot=False,
                author_role_names=["Sergeant", "GNT"],
                content="Revise la cronología.",
                created_at=now,
            ),
            ConversationMessage(
                id=11,
                author_id=30,
                author_display="Sargento Gómez",
                author_is_bot=False,
                author_role_names=["Sergeant", "GNT"],
                content="También falta una referencia.",
                created_at=now,
            ),
            ConversationMessage(
                id=12,
                author_id=40,
                author_display="Teniente Rivera",
                author_is_bot=False,
                author_role_names=["Lieutenant", "DLM"],
                content="Estoy de acuerdo con la observación.",
                created_at=now,
            ),
            ConversationMessage(
                id=13,
                author_id=50,
                author_display="Bot externo",
                author_is_bot=True,
                author_role_names=["Integración"],
                content="Mensaje automático.",
                created_at=now,
            ),
            ConversationMessage(
                id=14,
                author_id=20,
                author_display="Deputy Torres",
                author_is_bot=False,
                author_role_names=["Deputy Sheriff"],
                content="¿Cómo lo corrijo?",
                created_at=now,
                is_trigger=True,
            ),
        ],
        attachments=[],
    )

    rendered = render_agent_input(case, "original")
    trigger_text = rendered[-1]["content"][0]["text"]

    assert (
        "Otros participantes y roles: Sargento Gómez: Sergeant, GNT | "
        "Teniente Rivera: Lieutenant, DLM"
    ) in trigger_text
    assert "20=Deputy Torres" in trigger_text
    assert "30=Sargento Gómez" in trigger_text
    assert "40=Teniente Rivera" in trigger_text
    assert trigger_text.count("Sargento Gómez: Sergeant, GNT") == 1
    assert "Bot externo: Integración" not in trigger_text


def test_render_sends_supported_documents_as_file_url_inputs() -> None:
    attachments = [
        DiscordAttachment(
            id=index,
            message_id=10,
            filename=filename,
            url=f"https://cdn.discord.test/{filename}?signed=true",
            content_type=content_type,
            size=1000,
            is_current=True,
            input_kind="file",
        )
        for index, (filename, content_type) in enumerate(
            [
                ("reporte.txt", "text/plain"),
                ("expediente.pdf", "application/pdf"),
                (
                    "cargos.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ],
            start=1,
        )
    ]
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=3,
        trigger_message_id=10,
        requester_user_id=20,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=attachments,
    )

    content = render_agent_input(case, "original")[0]["content"]
    files = [part for part in content if part["type"] == "input_file"]
    labels = [part["text"] for part in content if part["type"] == "input_text"][1:]

    assert len(files) == 3
    assert files == [
        {
            "type": "input_file",
            "file_url": attachment.url,
        }
        for attachment in attachments
    ]
    assert all("Archivo adjunto" in label for label in labels)
    assert not any(part["type"] == "input_image" for part in content)


def test_attachment_selection_includes_files_and_rejects_unsupported_types(settings, persona) -> None:
    def attachment(
        attachment_id: int,
        filename: str,
        content_type: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=attachment_id,
            filename=filename,
            content_type=content_type,
            size=1000,
            url=f"https://cdn.discord.test/{filename}",
            width=100 if content_type.startswith("image/") else None,
            height=100 if content_type.startswith("image/") else None,
        )

    author = SimpleNamespace(id=20, display_name="Deputy", name="Deputy", bot=False)
    root = SimpleNamespace(
        id=1,
        attachments=[attachment(1, "suspect.png", "image/png")],
        author=author,
    )
    trigger = SimpleNamespace(
        id=2,
        attachments=[
            attachment(2, "reporte.txt", "text/plain"),
            attachment(3, "material.zip", "application/zip"),
        ],
        author=author,
    )
    target = ReviewTarget(
        root=root,
        trigger=trigger,
        parent=SimpleNamespace(id=10),
        thread=SimpleNamespace(id=11),
    )

    selected, notices = DiscordContextBuilder(settings, persona)._select_attachments(
        [root, trigger],
        target,
    )

    assert [item.id for item in selected] == [2, 1]
    assert [item.input_kind for item in selected] == ["file", "image"]
    assert selected[0].is_current is True
    assert selected[1].is_current is False
    assert notices == ["Se omitió el archivo no compatible 'material.zip'."]


def test_attachment_selection_limits_historical_files_separately_from_images(
    settings, persona
) -> None:
    settings.thread_context_max_historical_files = 1
    settings.thread_context_max_historical_images = 1

    def message(message_id: int, filename: str, content_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            attachments=[
                SimpleNamespace(
                    id=message_id,
                    filename=filename,
                    content_type=content_type,
                    size=1000,
                    url=f"https://cdn.discord.test/{filename}",
                    width=100 if content_type.startswith("image/") else None,
                    height=100 if content_type.startswith("image/") else None,
                )
            ],
        )

    root = message(1, "root.txt", "text/plain")
    old_file = message(2, "old.txt", "text/plain")
    recent_file = message(3, "recent.pdf", "application/pdf")
    old_image = message(4, "old.png", "image/png")
    recent_image = message(5, "recent.png", "image/png")
    trigger = message(6, "current.txt", "text/plain")
    target = ReviewTarget(
        root=root,
        trigger=trigger,
        parent=SimpleNamespace(id=10),
        thread=SimpleNamespace(id=11),
    )

    selected, notices = DiscordContextBuilder(settings, persona)._select_attachments(
        [root, old_file, recent_file, old_image, recent_image, trigger],
        target,
    )

    assert [item.id for item in selected] == [6, 1, 5, 3]
    assert "Se limitaron los archivos históricos del hilo." in notices
    assert "Se limitaron las imágenes históricas del hilo." in notices


def test_bot_embed_content_is_preserved_in_thread_history(settings, persona) -> None:
    author = SimpleNamespace(id=999, display_name="Sgt. Vigil", name="Sgt. Vigil", bot=True)
    embed = SimpleNamespace(
        title="❌ Correcciones requeridas",
        description="La narrativa necesita un ajuste material.",
        fields=[
            SimpleNamespace(
                name="🔴 Bolsa de evidencias",
                value="Falta el número de bolsa.\n**Cómo corregirlo:** incorpóralo al cierre.",
            )
        ],
        footer=SimpleNamespace(text="Watch Sergeant"),
    )
    message = SimpleNamespace(
        id=1,
        content="",
        author=author,
        created_at=datetime.now(UTC),
        edited_at=None,
        reference=None,
        attachments=[],
        embeds=[embed],
    )
    target = ReviewTarget(
        root=message,
        trigger=message,
        parent=SimpleNamespace(id=2),
        thread=SimpleNamespace(id=3),
    )

    converted, truncated = DiscordContextBuilder(settings, persona)._convert_messages(
        [message], target
    )

    assert truncated is False
    assert "Título: ❌ Correcciones requeridas" in converted[0].content
    assert "🔴 Bolsa de evidencias: Falta el número de bolsa." in converted[0].content
    assert "Pie: Watch Sergeant" in converted[0].content


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

    rendered = render_agent_input(case, "original")
    text = "\n".join(item["content"][0]["text"] for item in rendered)

    assert [item["role"] for item in rendered] == ["user", "user"]
    assert "EDITADO" in text
    assert "respuesta_a=4" in text
    assert "Falta justificar el cargo" in text
    assert "Entorno de Discord: hilo; la respuesta se publicará en este mismo hilo." in text


def test_render_describes_inline_reply_without_delegating_routing() -> None:
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=5,
        trigger_message_id=5,
        requester_user_id=20,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        response_route="inline_reply",
        messages=[],
        attachments=[],
    )

    text = render_agent_input(case, "original")[0]["content"][0]["text"]

    assert (
        "Entorno de Discord: canal; la respuesta se publicará como reply al mensaje actual."
        in text
    )


def test_context_trimming_preserves_root_and_current_request(settings, persona) -> None:
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
    builder = DiscordContextBuilder(settings, persona)

    converted, truncated = builder._convert_messages([root, old, trigger], target)

    assert truncated
    assert {item.id for item in converted} == {1, 3}
    assert sum(len(item.content) for item in converted) <= 500


@pytest.mark.asyncio
async def test_response_route_is_derived_from_activation_and_channel(
    settings, persona, monkeypatch
) -> None:
    class FakeTextChannel:
        def __init__(self) -> None:
            self.id = 20

    class FakeThread:
        def __init__(self, parent: FakeTextChannel) -> None:
            self.id = 30
            self.parent = parent

    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.Thread",
        FakeThread,
    )
    builder = DiscordContextBuilder(settings, persona)
    parent = FakeTextChannel()
    mention = SimpleNamespace(channel=parent, thread=None)
    reply = SimpleNamespace(channel=parent, thread=None)

    mention_target = await builder.resolve_target(mention, TriggerActivation.MENTION)
    reply_target = await builder.resolve_target(reply, TriggerActivation.BOT_REPLY)

    assert mention_target.response_route is DiscordResponseRoute.AGENT_CHOICE
    assert reply_target.response_route is DiscordResponseRoute.INLINE_REPLY


@pytest.mark.asyncio
async def test_orphaned_thread_uses_oldest_visible_message_as_root(
    settings, persona, monkeypatch, caplog
) -> None:
    class FakeDiscordError(Exception):
        pass

    class FakeTextChannel:
        id = 20

        async def fetch_message(self, message_id: int):
            assert message_id == 30
            raise FakeDiscordError("starter deleted")

    oldest = SimpleNamespace(id=31)

    class FakeThread:
        id = 30

        def __init__(self, parent: FakeTextChannel) -> None:
            self.parent = parent

        async def history(self, *, limit: int, oldest_first: bool):
            assert limit == 1
            assert oldest_first is True
            yield oldest

    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.Thread",
        FakeThread,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.NotFound",
        FakeDiscordError,
    )
    parent = FakeTextChannel()
    thread = FakeThread(parent)
    trigger = SimpleNamespace(
        id=32,
        channel=thread,
        guild=SimpleNamespace(id=10),
    )

    with caplog.at_level("WARNING"):
        target = await DiscordContextBuilder(settings, persona).resolve_target(
            trigger,
            TriggerActivation.MENTION,
        )

    assert target.root is oldest
    assert target.thread is thread
    assert target.response_route is DiscordResponseRoute.THREAD
    assert any(record.message == "thread_root_fallback_used" for record in caplog.records)


@pytest.mark.asyncio
async def test_empty_orphaned_thread_falls_back_to_trigger_message(
    settings, persona, monkeypatch
) -> None:
    class FakeDiscordError(Exception):
        pass

    class FakeTextChannel:
        id = 20

        async def fetch_message(self, message_id: int):
            raise FakeDiscordError("starter deleted")

    class FakeThread:
        id = 30

        def __init__(self, parent: FakeTextChannel) -> None:
            self.parent = parent

        async def history(self, *, limit: int, oldest_first: bool):
            if False:
                yield None

    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.Thread",
        FakeThread,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.context.discord.NotFound",
        FakeDiscordError,
    )
    parent = FakeTextChannel()
    thread = FakeThread(parent)
    trigger = SimpleNamespace(
        id=32,
        channel=thread,
        guild=SimpleNamespace(id=10),
    )

    target = await DiscordContextBuilder(settings, persona).resolve_target(
        trigger,
        TriggerActivation.MENTION,
    )

    assert target.root is trigger
    assert target.thread is thread
    assert target.response_route is DiscordResponseRoute.THREAD


@pytest.mark.asyncio
async def test_existing_thread_history_uses_message_limit_and_preserves_root_and_trigger(
    settings,
    persona,
) -> None:
    settings.thread_context_max_messages = 2

    def message(message_id: int, content: str, *, bot: bool = False):
        author = SimpleNamespace(
            id=10 if not bot else 999,
            display_name="Deputy" if not bot else "Sgt. Vigil",
            name="Deputy" if not bot else "Sgt. Vigil",
            bot=bot,
            roles=[],
        )
        return SimpleNamespace(
            id=message_id,
            content=content,
            author=author,
            created_at=datetime.fromtimestamp(message_id, tz=UTC),
            edited_at=None,
            reference=None,
            attachments=[],
            raw_mentions=[],
        )

    root = message(1, "Reporte raíz")
    root.guild = SimpleNamespace(id=1)
    previous = message(2, "Observación anterior", bot=True)
    trigger = message(3, "¿Y este cargo, sargento?")

    class FakeThread:
        id = 30

        async def history(self, *, limit: int, oldest_first: bool):
            assert limit == 2
            assert oldest_first is False
            for item in (trigger, previous):
                yield item

    thread = FakeThread()
    root.channel = SimpleNamespace(id=20)
    previous.channel = thread
    trigger.channel = thread
    target = ReviewTarget(
        root=root,
        trigger=trigger,
        parent=SimpleNamespace(id=20),
        thread=thread,
        response_route=DiscordResponseRoute.THREAD,
    )

    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    case = await DiscordContextBuilder(settings, persona, bot).build_case(target)

    assert [item.id for item in case.messages] == [1, 2, 3]
    assert case.context_state is ContextState.TRUNCATED
    assert case.context_notice == "El historial fue limitado a los mensajes más recientes."
    assert case.response_route == "thread"
    rendered = render_agent_input(case, "original")
    assert [item["role"] for item in rendered] == ["user", "assistant", "user"]
    assert "RESPUESTA_DEL_AGENTE" in rendered[1]["content"][0]["text"]
    assert rendered[1]["content"][0] == {
        "type": "output_text",
        "text": rendered[1]["content"][0]["text"],
        "annotations": [],
    }


@pytest.mark.asyncio
async def test_channel_context_includes_bounded_prior_text_without_historical_attachments(
    settings,
    persona,
) -> None:
    settings.channel_context_max_messages = 2

    def channel_message(
        message_id: int,
        content: str,
        *,
        attachments: list[SimpleNamespace] | None = None,
    ) -> SimpleNamespace:
        author = SimpleNamespace(
            id=message_id + 100,
            display_name=f"Deputy {message_id}",
            name=f"Deputy {message_id}",
            bot=False,
            roles=[],
        )
        return SimpleNamespace(
            id=message_id,
            content=content,
            author=author,
            created_at=datetime.fromtimestamp(message_id, tz=UTC),
            edited_at=None,
            reference=None,
            attachments=attachments or [],
            embeds=[],
            raw_mentions=[],
        )

    historical_attachment = SimpleNamespace(
        id=501,
        filename="captura-anterior.png",
        content_type="image/png",
    )
    oldest = channel_message(1, "¿Dónde aparece esa regla?")
    newest = channel_message(
        2,
        "Está en la guía de reportes.",
        attachments=[historical_attachment],
    )
    trigger = channel_message(3, "¿Me explicas eso?")
    trigger.guild = SimpleNamespace(id=10)

    class FakeTextChannel:
        id = 20

        async def history(self, *, limit: int, before: object, oldest_first: bool):
            assert limit == 2
            assert before is trigger
            assert oldest_first is False
            for item in (newest, oldest):
                yield item

    parent = FakeTextChannel()
    oldest.channel = parent
    newest.channel = parent
    trigger.channel = parent
    target = ReviewTarget(
        root=trigger,
        trigger=trigger,
        parent=parent,
        thread=None,
        response_route=DiscordResponseRoute.AGENT_CHOICE,
    )

    case = await DiscordContextBuilder(settings, persona).build_case(target)

    assert [item.id for item in case.messages] == [1, 2, 3]
    assert all(not item.attachment_ids for item in case.messages)
    assert case.messages[1].ambient_attachment_summary == (
        "1 imagen (captura-anterior.png)"
    )
    assert case.attachments == []
    assert case.context_state is ContextState.TRUNCATED
    assert case.context_notice == (
        "El contexto del canal fue limitado a los mensajes más recientes."
    )
    rendered = render_agent_input(case, "original")
    assert "CONTEXTO_PREVIO_CANAL" in rendered[0]["content"][0]["text"]
    assert "CONTEXTO_PREVIO_CANAL" in rendered[1]["content"][0]["text"]
    assert "contenido y URL omitidos" in rendered[1]["content"][0]["text"]
    assert "captura-anterior.png" in rendered[1]["content"][0]["text"]
    trigger_text = rendered[2]["content"][0]["text"]
    assert "contexto ambiental" in trigger_text
    assert "no mezcles conversaciones ni expedientes" in trigger_text


@pytest.mark.asyncio
async def test_channel_context_can_be_disabled(settings, persona) -> None:
    settings.channel_context_max_messages = 0
    trigger = SimpleNamespace(
        id=3,
        content="Consulta actual",
        author=SimpleNamespace(
            id=103,
            display_name="Deputy",
            name="Deputy",
            bot=False,
            roles=[],
        ),
        created_at=datetime.fromtimestamp(3, tz=UTC),
        edited_at=None,
        reference=None,
        attachments=[],
        embeds=[],
        raw_mentions=[],
        guild=SimpleNamespace(id=10),
    )

    class UnexpectedHistoryChannel:
        id = 20

        def history(self, **kwargs):
            raise AssertionError("channel history must not be read when disabled")

    parent = UnexpectedHistoryChannel()
    trigger.channel = parent
    target = ReviewTarget(
        root=trigger,
        trigger=trigger,
        parent=parent,
        thread=None,
    )

    case = await DiscordContextBuilder(settings, persona).build_case(target)

    assert [item.id for item in case.messages] == [3]
    assert case.context_state is ContextState.COMPLETE


def test_only_authenticated_discord_bot_is_rendered_as_assistant(settings, persona) -> None:
    def message(message_id: int, author_id: int, *, bot: bool) -> SimpleNamespace:
        author = SimpleNamespace(
            id=author_id,
            display_name=f"Autor {author_id}",
            name=f"Autor {author_id}",
            bot=bot,
        )
        return SimpleNamespace(
            id=message_id,
            content=f"Mensaje {message_id}",
            author=author,
            created_at=datetime.fromtimestamp(message_id, tz=UTC),
            edited_at=None,
            reference=None,
            attachments=[],
            embeds=[],
        )

    human = message(1, 10, bot=False)
    own_bot = message(2, 999, bot=True)
    external_bot = message(3, 555, bot=True)
    trigger = message(4, 10, bot=False)
    target = ReviewTarget(
        root=human,
        trigger=trigger,
        parent=SimpleNamespace(id=20),
        thread=SimpleNamespace(id=30),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    builder = DiscordContextBuilder(settings, persona, bot)

    converted, _ = builder._convert_messages(
        [human, own_bot, external_bot, trigger],
        target,
    )
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=20,
        channel_id=30,
        thread_id=30,
        root_message_id=1,
        trigger_message_id=4,
        requester_user_id=10,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=converted,
        attachments=[],
    )

    rendered = render_agent_input(case, "original")

    assert [item.author_is_assistant for item in converted] == [False, True, False, False]
    assert [item["role"] for item in rendered] == ["user", "assistant", "user", "user"]
    assert "BOT_EXTERNO" in rendered[2]["content"][0]["text"]
    assert rendered[1]["content"][0]["type"] == "output_text"
    assert rendered[2]["content"][0]["type"] == "input_text"


def test_attachments_remain_with_their_originating_conversation_turn() -> None:
    image = DiscordAttachment(
        id=101,
        message_id=1,
        filename="suspect.png",
        url="https://cdn.discord.test/suspect.png",
        content_type="image/png",
        size=1000,
        width=100,
        height=200,
        input_kind="image",
    )
    document = DiscordAttachment(
        id=303,
        message_id=3,
        filename="reporte-corregido.txt",
        url="https://cdn.discord.test/reporte-corregido.txt",
        content_type="text/plain",
        size=1000,
        is_current=True,
        input_kind="file",
    )
    messages = [
        ConversationMessage(
            id=1,
            author_id=10,
            author_display="Deputy",
            author_is_bot=False,
            content="Reporte inicial",
            created_at=datetime.now(UTC),
        ),
        ConversationMessage(
            id=2,
            author_id=999,
            author_display="Sgt. Vigil",
            author_is_bot=True,
            author_is_assistant=True,
            content="Debe corregir la narrativa.",
            created_at=datetime.now(UTC),
        ),
        ConversationMessage(
            id=3,
            author_id=10,
            author_display="Deputy",
            author_is_bot=False,
            content="Adjunto la versión corregida.",
            created_at=datetime.now(UTC),
            is_trigger=True,
        ),
    ]
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=20,
        channel_id=30,
        thread_id=30,
        root_message_id=1,
        trigger_message_id=3,
        requester_user_id=10,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=messages,
        attachments=[document, image],
    )

    rendered = render_agent_input(case, "original")

    assert [item["role"] for item in rendered] == ["user", "assistant", "user"]
    assert [part["type"] for part in rendered[0]["content"]] == [
        "input_text",
        "input_text",
        "input_image",
    ]
    assert [part["type"] for part in rendered[1]["content"]] == ["output_text"]
    assert [part["type"] for part in rendered[2]["content"]] == [
        "input_text",
        "input_text",
        "input_file",
    ]
    assert "[CONTEXTO_OPERATIVO]" not in rendered[0]["content"][0]["text"]
    assert "[CONTEXTO_OPERATIVO]" in rendered[2]["content"][0]["text"]


def test_assistant_attachments_are_reintroduced_as_user_input_context() -> None:
    attachment = DiscordAttachment(
        id=202,
        message_id=2,
        filename="agent-note.txt",
        url="https://cdn.discord.test/agent-note.txt",
        content_type="text/plain",
        size=1000,
        input_kind="file",
    )
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=20,
        channel_id=30,
        thread_id=30,
        root_message_id=1,
        trigger_message_id=3,
        requester_user_id=10,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[
            ConversationMessage(
                id=2,
                author_id=999,
                author_display="Sgt. Vigil",
                author_is_bot=True,
                author_is_assistant=True,
                content="Adjunto una referencia.",
                created_at=datetime.now(UTC),
            ),
            ConversationMessage(
                id=3,
                author_id=10,
                author_display="Deputy",
                author_is_bot=False,
                content="Revísela, sargento.",
                created_at=datetime.now(UTC),
                is_trigger=True,
            ),
        ],
        attachments=[attachment],
    )

    rendered = render_agent_input(case, "original")

    assert [item["role"] for item in rendered] == ["assistant", "user", "user"]
    assert [part["type"] for part in rendered[0]["content"]] == ["output_text"]
    assert [part["type"] for part in rendered[1]["content"]] == [
        "input_text",
        "input_text",
        "input_file",
    ]


@pytest.mark.asyncio
async def test_context_includes_only_brief_relevant_requester_role_names(settings, persona) -> None:
    settings.channel_context_max_messages = 0

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

    case = await DiscordContextBuilder(settings, persona).build_case(target)

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
    assert case.messages[0].author_role_names == case.requester_role_names
    rendered = render_agent_input(case, "original")[0]["content"][0]["text"]
    assert (
        "Roles del solicitante: Rol 10, Rol 9, Rol 8, Rol 7, Rol 6, Rol 5, Rol 4, Rol 3"
        in rendered
    )
    assert "Integración" not in rendered
    assert "@everyone" not in rendered
    assert "role_id" not in rendered
