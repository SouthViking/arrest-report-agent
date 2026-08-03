"""Typed tools exposed to the OpenAI agent."""

from __future__ import annotations

from typing import Annotated

from agents import RunContextWrapper, function_tool
from pydantic import Field

from arrest_report_agent.domain import ReviewPublication
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

        del ctx
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

        del ctx
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

        del ctx
        return catalog.get_details(charge_ids)

    @function_tool(failure_error_function=None)
    async def begin_review(
        ctx: RunContextWrapper[ReviewRunContext], case_subject: str | None = None
    ) -> dict[str, object]:
        """Mark an actual report review as started. Do not use for questions or missing material.

        Args:
            case_subject: Suspect or case label when confidently available.
        """

        run_context = ctx.context
        if run_context.started:
            return {"ok": True, "already_started": True}
        if case_subject:
            run_context.case.case_subject = case_subject[:80]
        await run_context.publisher.begin(run_context.case.case_subject)
        run_context.started = True
        return {"ok": True, "root_message_id": str(run_context.case.root_message_id)}

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
    async def reply_to_user(
        ctx: RunContextWrapper[ReviewRunContext], content: str
    ) -> dict[str, object]:
        """Send a natural response to the user. Use this as the terminal tool outside a review.

        Args:
            content: The complete response, with any useful Discord markdown and moderate emojis.
        """

        run_context = ctx.context
        if run_context.replied:
            return {"ok": True, "already_replied": True}
        if run_context.published:
            return {"ok": True, "already_completed": "publish_review"}
        await run_context.publisher.reply(content)
        run_context.replied = True
        return {"ok": True}

    return [
        begin_review,
        get_report_guide,
        list_charge_index,
        get_charge_details,
        reply_to_user,
        publish_review,
    ]
