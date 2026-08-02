"""Immediate Discord side effects used directly by agent tools."""

from __future__ import annotations

import contextlib
import re

import discord

from arrest_report_agent.config import AppSettings
from arrest_report_agent.domain import ErrorKind, FindingSeverity, ReviewPublication, ReviewVerdict
from arrest_report_agent.domain.errors import PublicationError

from .context import ReviewTarget

STATUS_EMOJIS = ("⏳", "✅", "❌", "⚠️")


class DiscordReviewPublisher:
    def __init__(self, bot: discord.Bot, settings: AppSettings, target: ReviewTarget) -> None:
        self.bot = bot
        self.settings = settings
        self.target = target
        self._thread = target.thread
        self._manual_role_pinged = False

    async def begin(self, case_subject: str | None = None) -> None:
        del case_subject
        try:
            await self.target.root.add_reaction("⏳")
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to add review reaction") from error

    async def publish(self, publication: ReviewPublication) -> None:
        await self._clear_bot_status_reactions()
        if publication.verdict is ReviewVerdict.APPROVED:
            await self.target.root.add_reaction("✅")
            if self.target.thread is not None:
                thread = await self._ensure_thread()
                await thread.send(embeds=build_review_embeds(publication, self.settings))
                await self._archive_bot_owned_thread(thread)
            return

        reaction = "❌" if publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED else "⚠️"
        await self.target.root.add_reaction(reaction)
        thread = await self._ensure_thread()
        mention = None
        if publication.verdict is ReviewVerdict.INCONCLUSIVE:
            mention = self._manual_role_mention()
        await thread.send(
            content=mention,
            embeds=build_review_embeds(publication, self.settings),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

    async def publish_failure(
        self,
        kind: ErrorKind,
        public_message: str,
        technical_reference: str,
    ) -> None:
        await self._clear_bot_status_reactions()
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await self.target.root.add_reaction("⚠️")
        thread = await self._ensure_thread()
        embed = discord.Embed(
            title=f"⚠️ Incidencia de revisión · {self.settings.identity.display_name}",
            description=public_message,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Clasificación", value=kind.value, inline=True)
        embed.add_field(name="Referencia técnica", value=technical_reference, inline=True)
        embed.add_field(
            name="Siguiente paso",
            value="Reintentar cuando corresponda o derivar el expediente a un supervisor.",
            inline=False,
        )
        await thread.send(
            content=self._manual_role_mention(),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

    async def _ensure_thread(self) -> discord.Thread:
        if self._thread is not None:
            if self._thread.archived:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await self._thread.edit(archived=False)
            return self._thread
        if self.target.root.thread is not None:
            self._thread = self.target.root.thread
            return self._thread
        subject = _safe_thread_name(self.target.root.content, self.target.root.id)
        try:
            self._thread = await self.target.root.create_thread(
                name=subject,
                auto_archive_duration=self.settings.thread_auto_archive_minutes,
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to create review thread") from error
        return self._thread

    async def _clear_bot_status_reactions(self) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return
        for emoji in STATUS_EMOJIS:
            with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.target.root.remove_reaction(emoji, bot_user)

    async def _archive_bot_owned_thread(self, thread: discord.Thread) -> None:
        bot_user = self.bot.user
        if bot_user is None or thread.owner_id != bot_user.id:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await thread.edit(archived=True, locked=True)

    def _manual_role_mention(self) -> str | None:
        role_id = self.settings.manual_review_role_id
        if role_id is None or self._manual_role_pinged:
            return None
        self._manual_role_pinged = True
        return f"<@&{role_id}>"


def build_review_embed(publication: ReviewPublication, settings: AppSettings) -> discord.Embed:
    """Build the primary embed; retained as a compact rendering helper for tests and previews."""

    return build_review_embeds(publication, settings)[0]


def build_review_embeds(
    publication: ReviewPublication, settings: AppSettings
) -> list[discord.Embed]:
    """Split a structured review into Discord-safe embeds without dropping findings."""

    colors = {
        ReviewVerdict.APPROVED: discord.Color.green(),
        ReviewVerdict.CORRECTIONS_REQUIRED: discord.Color.red(),
        ReviewVerdict.INCONCLUSIVE: discord.Color.orange(),
    }
    titles = {
        ReviewVerdict.APPROVED: "✅ Reporte aprobado",
        ReviewVerdict.CORRECTIONS_REQUIRED: "❌ Correcciones requeridas",
        ReviewVerdict.INCONCLUSIVE: "⚠️ Revisión inconclusa",
    }
    primary = discord.Embed(
        title=f"{titles[publication.verdict]} · {settings.identity.display_name}",
        description=publication.summary,
        color=colors[publication.verdict],
    )
    severity_icons = {
        FindingSeverity.REQUIRED: "🔴",
        FindingSeverity.WARNING: "🟠",
        FindingSeverity.OBSERVATION: "🔎",
    }
    embeds = [primary]
    for index, finding in enumerate(publication.findings):
        if index and index % 5 == 0:
            embeds.append(
                discord.Embed(
                    title=f"Continuación · {settings.identity.display_name}",
                    color=colors[publication.verdict],
                )
            )
        evidence = f"\n**Referencia:** {finding.evidence}" if finding.evidence else ""
        value = (
            f"{finding.detail}{evidence}\n**Cómo corregirlo:** {finding.recommendation}"
        )[:850]
        embeds[-1].add_field(
            name=f"{severity_icons[finding.severity]} {finding.title}"[:256],
            value=value,
            inline=False,
        )

    if publication.charges:
        charge_lines = [
            f"• **{charge.declared_charge}** — {charge.alignment}: {charge.note}"
            for charge in publication.charges
        ]
        charge_chunks = _chunk_lines(charge_lines, limit=1000)
        for group_start in range(0, len(charge_chunks), 5):
            charge_embed = discord.Embed(
                title="⚖️ Revisión de cargos",
                color=colors[publication.verdict],
            )
            for index, chunk in enumerate(charge_chunks[group_start : group_start + 5]):
                charge_embed.add_field(
                    name=(
                        "Cargos"
                        if group_start == 0 and index == 0
                        else "Cargos · continuación"
                    ),
                    value=chunk,
                    inline=False,
                )
            embeds.append(charge_embed)
    conclusion = discord.Embed(
        title="Cierre de la revisión",
        color=colors[publication.verdict],
    )
    conclusion.add_field(name="Siguiente paso", value=publication.next_step[:1024], inline=False)
    if publication.manual_review_reason:
        conclusion.add_field(
            name="Derivación manual",
            value=publication.manual_review_reason[:1024],
            inline=False,
        )
    conclusion.set_footer(text=settings.identity.role_title)
    embeds.append(conclusion)
    return embeds[:10]


def _safe_thread_name(content: str, message_id: int) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    without_mentions = re.sub(r"<@!?\d+>", "", first_line).strip()
    normalized = " ".join(without_mentions.split())
    return (normalized[:90] or f"Revisión {message_id}")


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        bounded_line = line[:limit]
        candidate = bounded_line if not current else f"{current}\n{bounded_line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = bounded_line
    if current:
        chunks.append(current)
    return chunks
