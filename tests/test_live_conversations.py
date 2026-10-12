from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.run_config import CallModelData, ModelInputData
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from arrest_report_agent.application.conversations import (
    ConversationKey,
    IncomingMessage,
    LiveConversation,
    live_message_id,
)
from arrest_report_agent.application.ports import MessageAttachmentLookupResult, MessageAttachmentProfile
from arrest_report_agent.application.service import ReviewService
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tool_errors import MAX_TOOL_ARGUMENT_RECOVERIES
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.config.logging import STRUCTURED_EVENT_ATTRIBUTE
from arrest_report_agent.config.review_failures import review_recovery_failure_messages
from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.infrastructure.conversations import InMemoryConversationStore
from arrest_report_agent.infrastructure.discord.live_messages import incoming_message
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide
from arrest_report_agent.review_analytics import RecordingReviewPublisher


def message(message_id=2, *, channel_id=20, text="Espera, ese reporte no era"):
    return IncomingMessage(
        message_id,
        30,
        json.dumps(
            {
                "message_id": str(message_id),
                "channel_id": str(channel_id),
                "author_id": "30",
                "content": text,
            }
        ),
    )


async def active(*, max_messages=100):
    store = InMemoryConversationStore(max_messages=max_messages)
    key = ConversationKey(10, 20)
    lease = await store.acquire(key, 1)
    assert lease is not None
    return store, key, LiveConversation(store, lease, key)


class MemorySession:
    session_id = "live-test"
    session_settings = None

    def __init__(self):
        self.items = []
        self.cursors = []

    async def get_items(self, limit=None):
        return deepcopy(self.items if limit is None else self.items[-limit:])

    async def add_items(self, items):
        self.items.extend(deepcopy(items))

    async def pop_item(self):
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.items.clear()

    def set_next_source_cursor(self, message_id):
        self.cursors.append(message_id)


@pytest.mark.asyncio
async def test_store_claim_is_atomic_and_scoped_by_guild_and_channel():
    store, key, live = await active()
    claims = await asyncio.gather(*(store.acquire(key, index) for index in range(10)))
    assert claims == [None] * 10
    assert await store.acquire(ConversationKey(11, 20), 1)
    assert await store.acquire(ConversationKey(10, 21), 1)
    assert await store.enqueue(key, message(1)) == "duplicate"
    assert await store.enqueue(key, message(3)) == "enqueued"
    assert await store.enqueue(key, message(2)) == "enqueued"
    assert await store.enqueue(key, message(2)) == "duplicate"
    assert not await store.close_if_empty(live.lease)
    assert [item.id for item in await store.drain(live.lease)] == [2, 3]
    assert await store.close_if_empty(live.lease)
    assert await store.enqueue(key, message(4)) == "inactive"
    newer = await store.acquire(key, 5)
    await store.release(live.lease)  # Old cleanup cannot release a new run.
    assert newer and await store.acquire(key, 6) is None


@pytest.mark.asyncio
async def test_snapshot_deduplication_aliases_and_bounded_lifetime():
    store, key, live = await active(max_messages=2)
    thread = ConversationKey(10, 40)
    assert await store.add_alias(live.lease, thread)
    assert await store.enqueue(thread, message(2)) == "enqueued"
    await store.discard_seen(live.lease, {1, 2})
    assert not await store.has_pending(live.lease)
    assert await store.enqueue(key, message(2)) == "duplicate"
    assert await store.enqueue(thread, message(3)) == "enqueued"
    await store.drain(live.lease)
    assert await store.enqueue(key, message(4)) == "full"
    other = await store.acquire(ConversationKey(10, 99), 50)
    assert not await store.add_alias(other, thread)
    await store.release(live.lease)
    assert await store.enqueue(thread, message(5)) == "inactive"


@pytest.mark.asyncio
async def test_filter_keeps_order_persists_once_and_logs_no_content(caplog):
    store, key, live = await active()
    session = MemorySession()
    live.begin_pass(session, 10)
    first = {"role": "user", "content": "original"}
    tool_result = {"type": "function_call_output", "call_id": "one", "output": "ok"}
    await store.enqueue(key, message(2, text="PRIVATE_CONTENT"))
    with caplog.at_level("INFO"):
        filtered = await live.filter_input(
            CallModelData(
                model_data=ModelInputData(input=[first], instructions="system"),
                agent=None,
                context=None,
            )
        )
    assert filtered.instructions == "system"
    assert [live_message_id(item) for item in filtered.input] == [None, 2]
    await store.enqueue(key, message(3))
    second = await live.filter_input(
        CallModelData(
            model_data=ModelInputData(input=[first, tool_result], instructions="system"),
            agent=None,
            context=None,
        )
    )
    assert [live_message_id(item) for item in second.input] == [None, 2, None, 3]
    third = await live.filter_input(
        CallModelData(
            model_data=ModelInputData(input=[first, tool_result], instructions="system"),
            agent=None,
            context=None,
        )
    )
    assert second.input == third.input
    assert [live_message_id(item) for item in session.items] == [2, 3]
    assert session.cursors == [2, 3]
    assert "PRIVATE_CONTENT" not in caplog.text
    assert any(getattr(record, STRUCTURED_EVENT_ATTRIBUTE, {}).get("count") == 1 for record in caplog.records)
    # A new Runner pass reads the durable session; the injection isn't repeated.
    live.begin_pass(session, 10)
    again = await live.filter_input(
        CallModelData(
            model_data=ModelInputData(input=third.input, instructions="system"),
            agent=None,
            context=None,
        )
    )
    assert again.input == third.input
    assert len(session.items) == 2


def test_discord_message_contains_ids_and_metadata_but_no_media_download():
    discord_message = SimpleNamespace(
        id=2,
        channel=SimpleNamespace(id=20),
        author=SimpleNamespace(id=30, display_name="Malcolm"),
        content="Estas son las fotos",
        reference=SimpleNamespace(message_id=1),
        attachments=[
            SimpleNamespace(
                id=4,
                filename="frontal.jpg",
                content_type="image/jpeg",
                size=123,
                url="https://private.test/file",
                read=AsyncMock(),
            )
        ],
        embeds=[],
    )
    rendered = incoming_message(discord_message, authorized=False)
    payload = json.loads(rendered.text)
    assert payload["message_id"] == "2"
    assert payload["channel_id"] == "20"
    assert payload["author_id"] == "30"
    assert payload["reply_to_message_id"] == "1"
    assert payload["authorized_staff"] is False
    assert payload["attachments"][0]["id"] == "4"
    assert "private.test" not in rendered.text
    discord_message.attachments[0].read.assert_not_called()


def response(text):
    return ModelResponse(
        output=[
            ResponseOutputMessage(
                id="msg_test",
                role="assistant",
                status="completed",
                type="message",
                content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
            )
        ],
        usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
        response_id=None,
    )


class ScriptedModel(Model):
    """Exercise the real SDK without OpenAI calls or credits."""

    def __init__(self, callback):
        self.callback = callback
        self.inputs = []

    async def get_response(self, **kwargs):
        self.inputs.append(deepcopy(kwargs["input"]))
        return await self.callback(len(self.inputs), kwargs["input"])

    async def stream_response(self, **kwargs):
        raise AssertionError("Not a streaming test")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_real_sdk_injects_after_tool_and_preserves_calls_and_live_items_in_session():
    store, key, live = await active()
    session = MemorySession()
    live.begin_pass(session, 10)

    @function_tool
    async def inspect_report() -> str:
        await store.enqueue(key, message(2))
        return "report found"

    async def model_call(number, items):
        if number == 1:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id="fc_one",
                        call_id="call_one",
                        name="inspect_report",
                        arguments="{}",
                        type="function_call",
                    )
                ],
                usage=Usage(requests=1),
                response_id=None,
            )
        assert any(live_message_id(item) == 2 for item in items)
        assert any(item.get("type") == "function_call_output" for item in items)
        return response("Revisión detenida")

    model = ScriptedModel(model_call)
    result = await Runner.run(
        Agent(name="test", model=model, tools=[inspect_report]),
        [{"role": "user", "content": "Revisa"}],
        session=session,
        run_config=RunConfig(tracing_disabled=True, call_model_input_filter=live.filter_input),
    )
    assert result.final_output == "Revisión detenida"
    assert len(model.inputs) == 2
    assert [live_message_id(item) for item in session.items].count(2) == 1
    kinds = [item.get("type") for item in session.items]
    index = next(i for i, item in enumerate(session.items) if live_message_id(item) == 2)
    assert kinds.index("function_call_output") < index < len(session.items) - 1


def review_context(live):
    publisher = SimpleNamespace(
        send_message=AsyncMock(),
        publish_failure=AsyncMock(),
        publish=AsyncMock(),
        mark_review_started=AsyncMock(),
        mention_roles=AsyncMock(),
        set_message_reaction=AsyncMock(),
    )
    case = ReviewCase(
        guild_id=10,
        parent_channel_id=19,
        channel_id=20,
        thread_id=20,
        root_message_id=1,
        trigger_message_id=1,
        requester_user_id=30,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    return ReviewRunContext(case=case, publisher=publisher, attempt=1, live_conversation=live)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
async def test_message_during_final_answer_continues_same_session_and_sends_only_updated_answer(
    settings,
    model,
    persona,
    persistent,
):
    store, key, live = await active()
    context = review_context(live)
    context.access_level = AgentAccessLevel.REPORT_OWNER
    context.owner_round = 2
    session = MemorySession() if persistent else None

    async def model_call(number, items):
        if number == 1:
            await store.enqueue(key, message(2))
            return response("ESTO_NO_DEBE_PUBLICARSE")
        assert any(live_message_id(item) == 2 for item in items)
        return response("Me detuve; espero la corrección.")

    fake_model = ScriptedModel(model_call)
    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    service.report_owner_agent.model = fake_model
    await service.review(context, [{"role": "user", "content": "Revisa"}], session=session)
    context.publisher.send_message.assert_awaited_once_with("Me detuve; espero la corrección.")
    context.publisher.publish_failure.assert_not_called()
    assert context.completed and context.owner_round == 2
    assert len(fake_model.inputs) == 2
    assert await store.enqueue(key, message(3)) == "inactive"
    if session:
        assert [live_message_id(item) for item in session.items].count(2) == 1


@pytest.mark.asyncio
async def test_message_arriving_while_discord_sends_is_not_lost(settings, model, persona):
    store, key, live = await active()
    context = review_context(live)

    async def send(text):
        if text == "Primera respuesta":
            await store.enqueue(key, message(2))

    context.publisher.send_message.side_effect = send

    async def model_call(number, items):
        return response("Primera respuesta" if number == 1 else "Actualización")

    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    service.agent.model = ScriptedModel(model_call)
    await service.review(context, [{"role": "user", "content": "Revisa"}])
    assert [call.args[0] for call in context.publisher.send_message.await_args_list] == [
        "Primera respuesta",
        "Actualización",
    ]
    assert context.completed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["send_message", "execute_arrest_report_review", "approve_arrest_report"]
)
async def test_real_sdk_defers_outdated_publication_tool(settings, model, persona, tool_name):
    store, key, live = await active()
    context = review_context(live)
    arguments = {
        "send_message": {"content": "OUTDATED", "response_route": "reply"},
        "approve_arrest_report": {"summary": "OUTDATED"},
        "execute_arrest_report_review": {
            "publication": {
                "review_scope": "full",
                "verdict": "approved",
                "summary": "OUTDATED",
                "findings": [],
                "charges": [],
            }
        },
    }[tool_name]

    async def model_call(number, items):
        if number == 1:
            await store.enqueue(key, message(2))
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id="fc_publish",
                        call_id="call_publish",
                        name=tool_name,
                        arguments=json.dumps(arguments),
                        type="function_call",
                    )
                ],
                usage=Usage(requests=1),
                response_id=None,
            )
        assert any(live_message_id(item) == 2 for item in items)
        outputs = [item["output"] for item in items if item.get("type") == "function_call_output"]
        assert any("new_messages_pending" in str(output) for output in outputs)
        return response("Me detuve")

    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    service.agent.model = ScriptedModel(model_call)
    await service.review(context, [{"role": "user", "content": "Revisa"}])
    assert context.completed
    context.publisher.publish.assert_not_called()
    context.publisher.send_message.assert_awaited_once_with("Me detuve")
    context.publisher.publish_failure.assert_not_called()


@pytest.mark.asyncio
async def test_live_loop_cannot_extend_original_turn_budget(settings, model, persona):
    store, key, live = await active()
    context = review_context(live)

    async def model_call(number, items):
        await store.enqueue(key, message(number + 1))
        return response("Respuesta obsoleta")

    service = ReviewService(
        settings.model_copy(update={"max_agent_turns": 2}),
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )
    fake_model = ScriptedModel(model_call)
    service.agent.model = fake_model
    await service.review(context, [{"role": "user", "content": "Revisa"}])
    assert len(fake_model.inputs) == 2
    assert not context.completed
    context.publisher.send_message.assert_not_called()
    context.publisher.publish_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_session_failure_fallback_retains_received_messages(settings, model, persona):
    from arrest_report_agent.application.errors import AgentSessionUnavailableError

    store, key, live = await active()
    context = review_context(live)

    class BrokenLiveSession(MemorySession):
        async def add_items(self, items):
            if any(live_message_id(item) for item in items):
                raise AgentSessionUnavailableError("simulated write failure")
            await super().add_items(items)

    await store.enqueue(key, message(2))

    async def model_call(number, items):
        assert any(live_message_id(item) == 2 for item in items)
        return response("Corregido")

    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    service.agent.model = ScriptedModel(model_call)
    await service.review(
        context,
        [{"role": "user", "content": "Revisa"}],
        session=BrokenLiveSession(),
        stateless_agent_input=[{"role": "user", "content": "Contexto completo"}],
    )
    assert context.completed
    context.publisher.send_message.assert_awaited_once_with("Corregido")
    context.publisher.publish_failure.assert_not_called()


@pytest.mark.asyncio
async def test_successful_review_tool_does_not_duplicate_final_response(settings, model, persona):
    store, key, live = await active()
    context = review_context(live)

    async def model_call(number, items):
        if number == 1:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id="fc_publish",
                        call_id="call_publish",
                        name="approve_arrest_report",
                        arguments=json.dumps({"summary": "Aprobado"}),
                        type="function_call",
                    )
                ],
                usage=Usage(requests=1),
                response_id=None,
            )
        return response("Ya lo aprobé")

    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )
    service.agent.model = ScriptedModel(model_call)
    await service.review(context, [{"role": "user", "content": "Apruébalo"}])
    assert context.completed and context.published
    context.publisher.publish.assert_awaited_once()
    context.publisher.send_message.assert_not_called()
    context.publisher.publish_failure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("access_level", [AgentAccessLevel.REPORT_OWNER, AgentAccessLevel.FULL])
async def test_real_sdk_repairs_invalid_review_arguments_in_the_same_run(
    settings, model, persona, persistent, access_level
):
    store, key, live = await active()
    context = review_context(live)
    context.access_level = access_level
    context.owner_round = 2
    context.owner_max_rounds = 5
    session = MemorySession() if persistent else None
    corrected = json.dumps(
        {
            "publication": {
                "verdict": "approved",
                "summary": "El expediente es coherente.",
                "findings": [],
                "charges": [],
            }
        }
    )

    async def model_call(number, items):
        if number == 2:
            context.publisher.publish.assert_not_called()
            outputs = [item for item in items if item.get("type") == "function_call_output"]
            assert len(outputs) == 1 and outputs[0]["call_id"] == "call-review-1"
            assert json.loads(outputs[0]["output"])["error"] == "invalid_tool_arguments"
        if number <= 2:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id=f"fc-review-{number}",
                        call_id=f"call-review-{number}",
                        name="execute_arrest_report_review",
                        arguments='{"publication":' if number == 1 else corrected,
                        type="function_call",
                    )
                ],
                usage=Usage(requests=1),
                response_id=None,
            )
        return response("Revisión publicada")

    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    fake_model = ScriptedModel(model_call)
    service.agent.model = fake_model
    service.report_owner_agent.model = fake_model
    await service.review(context, [{"role": "user", "content": "Revisa"}], session=session)

    assert context.completed and context.published
    assert context.attempt == 1 and context.owner_round == 2 and context.owner_max_rounds == 5
    assert context.tool_argument_recovery_counts == {"execute_arrest_report_review": 1}
    assert len(fake_model.inputs) == 3
    assert live.model_calls == 3
    context.publisher.publish.assert_awaited_once()
    context.publisher.mark_review_started.assert_awaited_once()
    context.publisher.send_message.assert_not_called()
    context.publisher.publish_failure.assert_not_called()
    assert await store.enqueue(key, message(3)) == "inactive"
    if session is not None:
        outputs = [item for item in session.items if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["call-review-1", "call-review-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("access_level", [AgentAccessLevel.REPORT_OWNER, AgentAccessLevel.FULL])
async def test_real_sdk_stops_after_bounded_review_argument_recoveries(
    settings, model, persona, access_level
):
    _, _, live = await active()
    context = review_context(live)
    context.access_level = access_level
    delegate = context.publisher
    context.publisher = RecordingReviewPublisher(
        delegate,
        SimpleNamespace(),
        context.case,
        model=model.id,
        reasoning_effort=model.reasoning_effort,
        persona_id=persona.id,
        persona_revision=persona.revision,
        app_version="test",
    )

    async def model_call(number, items):
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    id=f"fc-review-{number}",
                    call_id=f"call-review-{number}",
                    name="execute_arrest_report_review",
                    arguments='{"publication":',
                    type="function_call",
                )
            ],
            usage=Usage(requests=1),
            response_id=None,
        )

    service = ReviewService(
        settings, ChargeCatalog.load_default(), ReportGuide.load_default(), model, persona
    )
    fake_model = ScriptedModel(model_call)
    service.agent.model = fake_model
    service.report_owner_agent.model = fake_model
    await service.review(context, [{"role": "user", "content": "Revisa"}])

    assert len(fake_model.inputs) == MAX_TOOL_ARGUMENT_RECOVERIES + 1
    assert not context.completed and not context.published
    assert context.tool_argument_recovery_exhausted
    delegate.publish.assert_not_called()
    delegate.mark_review_started.assert_not_called()
    delegate.publish_failure.assert_awaited_once_with(
        review_recovery_failure_messages(persona).description,
        notify_manual_review=True,
    )


@pytest.mark.asyncio
async def test_new_attachments_are_deferred_current_images_not_historical_and_still_bounded():
    store, key, live = await active()
    context = review_context(live)
    context.access_level = AgentAccessLevel.REPORT_OWNER
    new_message_id = 44444444444444444
    live.begin_pass(None, 10)
    await store.enqueue(key, message(new_message_id))
    await live.filter_input(
        CallModelData(
            model_data=ModelInputData(input=[], instructions=None),
            agent=None,
            context=None,
        )
    )
    directory = SimpleNamespace(
        lookup_message_attachments=AsyncMock(
            return_value=MessageAttachmentLookupResult(
                attachments=tuple(
                    MessageAttachmentProfile(
                        id=55555555555555550 + index,
                        filename=f"photo-{index}.jpg",
                        url=f"https://cdn.discord.test/{index}.jpg",
                        content_type="image/jpeg",
                        size=1000,
                        width=800,
                        height=600,
                        input_kind="image",
                    )
                    for index in range(3)
                ),
            )
        )
    )
    context.message_directory = directory
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            max_historical_images=0,
            max_images_per_review=2,
        )
    }
    arguments = json.dumps({"message_id": str(new_message_id)})
    ctx = ToolContext(
        context=context,
        tool_name="get_message_attachments",
        tool_call_id="attachments",
        tool_arguments=arguments,
    )
    result = await tools["get_message_attachments"].on_invoke_tool(ctx, arguments)
    metadata = json.loads(result[0].text)
    assert len(metadata["loaded"]) == 2
    assert len(metadata["skipped"]) == 1
    assert metadata["skipped"][0]["reason"] != "historical_image_limit"
    assert context.loaded_historical_image_count == 0
    again = await tools["get_message_attachments"].on_invoke_tool(ctx, arguments)
    assert json.loads(again[0].text)["loaded"] == []
