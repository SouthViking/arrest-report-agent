"""Typed tools exposed to the OpenAI agent."""

from __future__ import annotations

from typing import Annotated

from agents import RunContextWrapper, function_tool
from pydantic import Field

from arrest_report_agent.domain import AgentActivity, ReviewPublication
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeMatchMode,
    ChargeSearchField,
    ReportGuide,
)

from .service_context import ReviewRunContext


def build_agent_tools(catalog: ChargeCatalog, guide: ReportGuide) -> list[object]:
    """Create tools bound to immutable local knowledge sources."""

    @function_tool
    async def get_report_guide(
        ctx: RunContextWrapper[ReviewRunContext], sections: list[str] | None = None
    ) -> dict[str, object]:
        """Read selected manual sections. Omit sections to get the compact overview.

        Args:
            sections: Names from available_sections returned by this tool.
        """

        await _mark_knowledge_activity(ctx.context)
        return guide.get(sections)

    @function_tool
    async def list_charge_index(
        ctx: RunContextWrapper[ReviewRunContext],
        category_ids: Annotated[
            list[Annotated[int, Field(gt=0)]] | None,
            Field(max_length=14),
        ] = None,
        query: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        fields: Annotated[
            list[ChargeSearchField] | None,
            Field(min_length=1, max_length=5),
        ] = None,
        match_mode: ChargeMatchMode = "all",
        fuzzy: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        """List charge categories or search a configurable compact charge index.

        Args:
            category_ids: Optional category IDs to restrict the index.
            query: Spanish words or quoted phrases to find.
            fields: Fields to inspect; defaults to code, title, type and category.
            match_mode: Require all query terms or allow any one of them.
            fuzzy: Allow conservative typo matching for terms of four or more characters.
            limit: Maximum compact results, from 1 to 100.
        """

        await _mark_knowledge_activity(ctx.context)
        return catalog.list_index(
            category_ids=category_ids,
            query=query,
            fields=fields,
            match_mode=match_mode,
            fuzzy=fuzzy,
            limit=limit,
        )

    @function_tool
    async def get_charge_details(
        ctx: RunContextWrapper[ReviewRunContext], charge_ids: list[int]
    ) -> dict[str, object]:
        """Get authoritative descriptions and penalties for one to ten unique charge IDs.

        Args:
            charge_ids: Stable IDs obtained from list_charge_index, never ambiguous codes.
        """

        await _mark_knowledge_activity(ctx.context)
        return catalog.get_details(charge_ids)

    @function_tool(failure_error_function=None)
    async def publish_review(
        ctx: RunContextWrapper[ReviewRunContext], publication: ReviewPublication
    ) -> dict[str, object]:
        """Publish the final structured verdict to Discord. This is the terminal tool.

        Args:
            publication: Validated verdict, findings, charge assessments and next step.
        """

        run_context = ctx.context
        if run_context.published:
            return {"ok": True, "already_published": True}
        await run_context.publisher.publish(publication)
        run_context.published = True
        run_context.publication = publication
        return {"ok": True, "verdict": publication.verdict.value}

    @function_tool(failure_error_function=None)
    async def send_message(ctx: RunContextWrapper[ReviewRunContext], content: str) -> dict[str, object]:
        """Send an enriched intermediate Discord message and continue processing.

        Args:
            content: Progress or partial information using Discord Markdown when useful: headings,
                lists, quotes, emphasis, code, links and moderate emojis. Do not imitate embeds or
                include mass mentions. This tool does not finish the interaction and may be called
                more than once.
        """

        run_context = ctx.context
        if run_context.published:
            return {"ok": True, "already_completed": "publish_review"}
        if run_context.interaction_completed:
            return {"ok": True, "already_completed": "finish_interaction"}
        await run_context.publisher.send_message(content)
        run_context.message_sent = True
        run_context.messages_sent_count += 1
        return {
            "ok": True,
            "delivery": "discord_message",
            "messages_sent": run_context.messages_sent_count,
            "interaction_complete": False,
        }

    @function_tool(failure_error_function=None)
    async def finish_interaction(ctx: RunContextWrapper[ReviewRunContext], content: str) -> dict[str, object]:
        """Send the final conversational response and explicitly finish the interaction.

        Args:
            content: Complete final Discord message. It may be an answer, a clarification request or
                a handoff back to the user. Discord Markdown, links and moderate emojis are allowed;
                do not imitate embeds or include mass mentions.
        """

        run_context = ctx.context
        if run_context.published:
            return {"ok": True, "already_completed": "publish_review"}
        if run_context.interaction_completed:
            return {"ok": True, "already_completed": "finish_interaction"}
        await run_context.publisher.send_message(content)
        run_context.message_sent = True
        run_context.messages_sent_count += 1
        run_context.interaction_completed = True
        return {
            "ok": True,
            "delivery": "discord_message",
            "messages_sent": run_context.messages_sent_count,
            "interaction_complete": True,
        }

    return [
        get_report_guide,
        list_charge_index,
        get_charge_details,
        send_message,
        finish_interaction,
        publish_review,
    ]


async def _mark_knowledge_activity(run_context: ReviewRunContext) -> None:
    if run_context.activity is not None:
        await run_context.activity.promote(AgentActivity.KNOWLEDGE)
