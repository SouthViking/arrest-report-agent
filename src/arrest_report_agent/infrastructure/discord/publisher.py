"""Immediate Discord side effects used directly by agent tools."""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from arrest_report_agent.config import AppSettings, BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import (
    ConversationResponseRoute,
    FindingSeverity,
    ManualQuizDelivery,
    ManualQuizPublication,
    ReviewPublication,
    ReviewScope,
    ReviewVerdict,
)
from arrest_report_agent.domain.errors import PublicationError

from .context import DiscordResponseRoute, ReviewTarget

if TYPE_CHECKING:
    from arrest_report_agent.manual_quiz import ManualQuizDiscordPublisher, ManualQuizView

STATUS_EMOJIS = ("⏳", "✅", "❌", "⚠️")
MAX_ROLE_MENTIONS_PER_MESSAGE = 3
MAX_ROLE_NOTIFICATION_CONTENT = 1700
LOGGER = logging.getLogger(__name__)


class DiscordReviewPublisher:
    def __init__(
        self,
        bot: discord.Bot,
        settings: AppSettings,
        target: ReviewTarget,
        persona: BotPersona,
        *,
        allow_outgoing_mentions: bool = True,
        manual_quiz_publisher: ManualQuizDiscordPublisher | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.target = target
        self.persona = persona
        self._thread = target.thread
        self._escalation_roles_pinged = False
        self._allow_outgoing_mentions = allow_outgoing_mentions
        self._manual_quiz_publisher = manual_quiz_publisher
        self.on_thread_ready: Callable[[discord.Thread], Awaitable[None]] | None = None

    async def mark_review_started(self) -> None:
        try:
            await self.target.root.add_reaction("⏳")
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to add review reaction") from error

    async def publish(self, publication: ReviewPublication) -> None:
        if publication.review_scope is not ReviewScope.FULL:
            await self._clear_bot_status_reactions(("⏳",))
            mention = None
            allowed_mentions = discord.AllowedMentions.none()
            if publication.verdict is ReviewVerdict.INCONCLUSIVE:
                mention, allowed_mentions = self._escalation_notice()
            await self._send_routed_payload(
                content=mention,
                embeds=build_review_embeds(publication, self.persona),
                allowed_mentions=allowed_mentions,
            )
            return

        await self._clear_bot_status_reactions()
        if publication.verdict is ReviewVerdict.APPROVED:
            await self.target.root.add_reaction("✅")
            thread = await self._send_routed_payload(
                embeds=build_review_embeds(publication, self.persona),
            )
            await self._update_structured_report_card(publication)
            if thread is not None:
                await self._close_approved_thread(thread)
            return

        reaction = "❌" if publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED else "⚠️"
        await self.target.root.add_reaction(reaction)
        mention = None
        allowed_mentions = discord.AllowedMentions.none()
        if publication.verdict is ReviewVerdict.INCONCLUSIVE:
            mention, allowed_mentions = self._escalation_notice()
        await self._send_routed_payload(
            content=mention,
            embeds=build_review_embeds(publication, self.persona),
            allowed_mentions=allowed_mentions,
        )
        await self._update_structured_report_card(publication)

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        """Send plain enriched text through the effective safe Discord route."""

        rendered_content = content.strip()
        if not self._allow_outgoing_mentions:
            rendered_content = re.sub(r"<@&[1-9][0-9]{16,19}>", "rol", rendered_content)
            rendered_content = re.sub(r"<@!?[1-9][0-9]{16,19}>", "usuario", rendered_content)
        chunks = _chunk_text(rendered_content, limit=2000)
        if not chunks:
            raise PublicationError("Unable to publish an empty agent response")
        allowed_mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=True,
                roles=False,
                replied_user=False,
            )
            if self._allow_outgoing_mentions
            else discord.AllowedMentions.none()
        )
        try:
            effective_route = self._conversation_route(response_route)
            if effective_route is ConversationResponseRoute.THREAD:
                thread = await self._ensure_thread()
                for chunk in chunks:
                    await thread.send(
                        content=chunk,
                        allowed_mentions=allowed_mentions,
                    )
            else:
                await self.target.trigger.reply(
                    content=chunks[0],
                    mention_author=False,
                    allowed_mentions=allowed_mentions,
                )
                for chunk in chunks[1:]:
                    await self.target.trigger.channel.send(
                        content=chunk,
                        allowed_mentions=allowed_mentions,
                    )
            return effective_route
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to publish agent response") from error

    async def mention_roles(
        self,
        role_ids: tuple[int, ...],
        content: str | None = None,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        """Notify a bounded, explicitly resolved set of roles from the active guild."""

        if not self._allow_outgoing_mentions:
            raise PublicationError("Role mentions are unavailable for this access level")
        if (
            not role_ids
            or len(role_ids) > MAX_ROLE_MENTIONS_PER_MESSAGE
            or len(role_ids) != len(set(role_ids))
        ):
            raise PublicationError("Role mention selection is invalid")
        guild = self.target.root.guild
        roles: list[discord.Role] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None or role.id == guild.id or role.is_default():
                raise PublicationError("Requested role is unavailable for notification")
            roles.append(role)

        effective_route = self._conversation_route(response_route)
        destination: discord.TextChannel | discord.Thread = (
            self.target.thread
            if self.target.thread is not None
            else self.target.parent
        )
        if any(not _can_mention_role(destination, guild, role) for role in roles):
            raise PublicationError("Bot cannot mention one or more requested roles")

        rendered_content = _sanitize_role_notification_content(content)
        mention_line = " ".join(role.mention for role in roles)
        message = (
            f"{mention_line}\n\n{rendered_content}"
            if rendered_content is not None
            else mention_line
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=roles,
            replied_user=False,
        )
        try:
            if effective_route is ConversationResponseRoute.THREAD:
                thread = await self._ensure_thread()
                if any(not _can_mention_role(thread, guild, role) for role in roles):
                    raise PublicationError("Bot cannot mention one or more requested roles")
                await thread.send(content=message, allowed_mentions=allowed_mentions)
            else:
                await self.target.trigger.reply(
                    content=message,
                    mention_author=False,
                    allowed_mentions=allowed_mentions,
                )
        except PublicationError:
            raise
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to publish requested role notification") from error
        log_event(
            LOGGER,
            "info",
            "agent_roles_notified",
            guild_id=guild.id,
            parent_channel_id=self.target.parent.id,
            thread_id=(
                self._thread.id
                if effective_route is ConversationResponseRoute.THREAD and self._thread is not None
                else "none"
            ),
            root_message_id=self.target.root.id,
            trigger_message_id=self.target.trigger.id,
            requester_user_id=self.target.trigger.author.id,
            role_ids=list(role_ids),
            role_count=len(roles),
        )
        return effective_route

    async def set_message_reaction(
        self,
        message_id: int,
        emoji: str,
    ) -> str:
        """Apply one reaction within the active conversation and replace bot status markers."""

        message = await self._resolve_reaction_message(message_id)
        bot_user = self.bot.user
        if bot_user is None:
            raise PublicationError("Unable to resolve the bot user for a reaction")

        rendered_reactions = {str(reaction.emoji): reaction for reaction in message.reactions if reaction.me}
        removed_status = False
        try:
            if emoji in STATUS_EMOJIS:
                for current_emoji, reaction in rendered_reactions.items():
                    if current_emoji in STATUS_EMOJIS and current_emoji != emoji:
                        await message.remove_reaction(reaction.emoji, bot_user)
                        removed_status = True
            already_present = emoji in rendered_reactions
            if not already_present:
                await message.add_reaction(emoji)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            discord.InvalidArgument,
            TypeError,
        ) as error:
            raise PublicationError("Unable to update the requested Discord reaction") from error

        outcome = "replaced" if removed_status else "unchanged" if already_present else "added"
        log_event(
            LOGGER,
            "info",
            "agent_message_reaction_updated",
            guild_id=self.target.root.guild.id,
            parent_channel_id=self.target.parent.id,
            thread_id=self.target.thread.id if self.target.thread is not None else "none",
            root_message_id=self.target.root.id,
            trigger_message_id=self.target.trigger.id,
            target_message_id=message.id,
            requester_user_id=self.target.trigger.author.id,
            emoji=emoji,
            outcome=outcome,
        )
        return outcome

    async def publish_manual_quiz(
        self,
        publication: ManualQuizPublication,
    ) -> ManualQuizDelivery:
        """Publish an agent-authored question through the shared manual-quiz renderer."""

        quiz_publisher = self._manual_quiz_publisher
        if quiz_publisher is None:
            return ManualQuizDelivery(ok=False, status="unavailable")

        from arrest_report_agent.manual_quiz import (
            ActiveQuizSessionError,
            QuizCooldownError,
        )

        trigger = self.target.trigger
        author = trigger.author

        async def send_question(
            embed: discord.Embed,
            view: ManualQuizView,
        ) -> discord.Message:
            try:
                return await trigger.reply(
                    embed=embed,
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                raise PublicationError("Unable to publish a manual quiz") from error

        try:
            question = await quiz_publisher.publish_dynamic(
                guild_id=trigger.guild.id,
                channel_id=trigger.channel.id,
                publisher_id=author.id,
                publisher_name=getattr(author, "display_name", author.name),
                publisher_avatar_url=str(author.display_avatar.url),
                publication=publication,
                ui=self.persona.messages.interface.manual_quiz,
                sender=send_question,
            )
        except ActiveQuizSessionError:
            return ManualQuizDelivery(ok=False, status="active_question")
        except QuizCooldownError as error:
            return ManualQuizDelivery(
                ok=False,
                status="cooldown",
                retry_after_seconds=error.remaining_seconds,
            )
        return ManualQuizDelivery(
            ok=True,
            status="published",
            question_id=str(question.id),
        )

    async def _resolve_reaction_message(self, message_id: int) -> discord.Message:
        if message_id == self.target.root.id:
            return self.target.root
        if message_id == self.target.trigger.id:
            return self.target.trigger

        channel: discord.TextChannel | discord.Thread = (
            self.target.thread if self.target.thread is not None else self.target.parent
        )
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound as error:
            raise PublicationError(
                "Reaction target is not part of the active Discord conversation"
            ) from error
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to resolve the requested reaction target") from error

    def _conversation_route(
        self,
        requested_route: ConversationResponseRoute,
    ) -> ConversationResponseRoute:
        if self.target.thread is not None:
            return ConversationResponseRoute.THREAD
        if self.target.response_route is DiscordResponseRoute.THREAD:
            return ConversationResponseRoute.THREAD
        if self.target.response_route is DiscordResponseRoute.INLINE_REPLY:
            return ConversationResponseRoute.REPLY
        return requested_route

    async def _send_routed_payload(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> discord.Thread | None:
        """Send review output to a thread unless activation fixed an inline reply."""

        mentions = allowed_mentions or discord.AllowedMentions.none()
        try:
            if self.target.response_route is not DiscordResponseRoute.INLINE_REPLY:
                thread = await self._ensure_thread()
                await thread.send(
                    content=content,
                    embed=embed,
                    embeds=embeds,
                    allowed_mentions=mentions,
                )
                return thread
            await self.target.trigger.reply(
                content=content,
                embed=embed,
                embeds=embeds,
                mention_author=False,
                allowed_mentions=mentions,
            )
            return None
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to publish routed review output") from error

    async def publish_failure(
        self,
        public_message: str,
    ) -> None:
        await self._clear_bot_status_reactions()
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await self.target.root.add_reaction("⚠️")
        embed = discord.Embed(
            title=self.persona.messages.failures.review_failure_title,
            description=public_message,
            color=discord.Color.orange(),
        )
        embed.add_field(
            name=self.persona.messages.interface.review.failure_next_step_title,
            value=self.persona.messages.failures.review_failure_next_step,
            inline=False,
        )
        await self._send_routed_payload(
            embed=embed,
        )

    async def _ensure_thread(self) -> discord.Thread:
        if self._thread is not None:
            if self._thread.archived:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await self._thread.edit(archived=False)
            if self.on_thread_ready is not None:
                await self.on_thread_ready(self._thread)
            return self._thread
        if self.target.root.thread is not None:
            self._thread = self.target.root.thread
            if self.on_thread_ready is not None:
                await self.on_thread_ready(self._thread)
            return self._thread
        subject = _safe_thread_name(
            self.target.root.content,
            self.target.root.id,
            self.persona.messages.interface.review.fallback_thread_title,
        )
        try:
            self._thread = await self.target.root.create_thread(
                name=subject,
                auto_archive_duration=self.settings.thread_auto_archive_minutes,
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            raise PublicationError("Unable to create review thread") from error
        if self.on_thread_ready is not None:
            await self.on_thread_ready(self._thread)
        return self._thread

    async def _clear_bot_status_reactions(
        self,
        emojis: tuple[str, ...] = STATUS_EMOJIS,
    ) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return
        for emoji in emojis:
            with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.target.root.remove_reaction(emoji, bot_user)

    async def _close_approved_thread(self, thread: discord.Thread) -> None:
        try:
            await thread.edit(archived=True)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as error:
            log_event(
                LOGGER,
                "warning",
                "approved_review_thread_close_failed",
                exc_info=error,
                guild_id=getattr(getattr(self.target.root, "guild", None), "id", "unknown"),
                root_message_id=self.target.root.id,
                thread_id=thread.id,
                **exception_fields(error),
            )

    async def _update_structured_report_card(self, publication: ReviewPublication) -> None:
        """Best-effort update of the root card after a completed full review."""

        submission_id = self.target.structured_submission_id
        try:
            from arrest_report_agent.structured_arrest_reports.card import (  # noqa: PLC0415
                is_submission_card,
                recolor_submission_view,
            )

            if submission_id is None and not is_submission_card(self.target.root):
                return
            await self.target.root.edit(
                view=recolor_submission_view(self.target.root, publication.verdict),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "structured_arrest_report_card_updated",
                guild_id=getattr(getattr(self.target.root, "guild", None), "id", "unknown"),
                root_message_id=self.target.root.id,
                submission_id=str(submission_id) if submission_id is not None else "unmapped",
                review_scope=publication.review_scope.value,
                verdict=publication.verdict.value,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "structured_arrest_report_card_update_failed",
                exc_info=error,
                guild_id=getattr(getattr(self.target.root, "guild", None), "id", "unknown"),
                root_message_id=self.target.root.id,
                submission_id=str(submission_id) if submission_id is not None else "unmapped",
                review_scope=publication.review_scope.value,
                verdict=publication.verdict.value,
                **exception_fields(error),
            )

    def _escalation_notice(self) -> tuple[str | None, discord.AllowedMentions]:
        if not self._allow_outgoing_mentions:
            return None, discord.AllowedMentions.none()
        if self._escalation_roles_pinged:
            return None, discord.AllowedMentions.none()
        self._escalation_roles_pinged = True

        roles = self._resolve_escalation_roles()
        if not roles:
            return None, discord.AllowedMentions.none()
        mentions = " ".join(role.mention for role in roles)
        return (
            self.persona.messages.interface.review.escalation_notice.format(roles=mentions),
            discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=roles,
                replied_user=False,
            ),
        )

    def _resolve_escalation_roles(self) -> list[discord.Role]:
        role_ids = self.settings.review_escalation_role_ids
        if not role_ids:
            return []
        guild = self.target.root.guild
        roles: list[discord.Role] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                roles.append(role)
                continue
            log_event(
                LOGGER,
                "warning",
                "review_escalation_role_unavailable",
                guild_id=guild.id,
                root_message_id=self.target.root.id,
                role_id=role_id,
            )
        return roles


def build_review_embed(publication: ReviewPublication, persona: BotPersona) -> discord.Embed:
    """Build the primary embed; retained as a compact rendering helper for tests and previews."""

    return build_review_embeds(publication, persona)[0]


def build_review_embeds(publication: ReviewPublication, persona: BotPersona) -> list[discord.Embed]:
    """Split a structured review into Discord-safe embeds without dropping findings."""

    colors = {
        ReviewVerdict.APPROVED: discord.Color.green(),
        ReviewVerdict.CORRECTIONS_REQUIRED: discord.Color.red(),
        ReviewVerdict.INCONCLUSIVE: discord.Color.orange(),
    }
    ui = persona.messages.interface.review
    titles = {
        ReviewVerdict.APPROVED: ui.approved_title,
        ReviewVerdict.CORRECTIONS_REQUIRED: ui.corrections_required_title,
        ReviewVerdict.INCONCLUSIVE: ui.inconclusive_title,
    }
    title = titles[publication.verdict]
    scope_labels = {
        ReviewScope.NARRATIVE: ui.narrative_scope_label,
        ReviewScope.CHARGES: ui.charges_scope_label,
        ReviewScope.TARGETED: ui.targeted_scope_label,
    }
    if publication.review_scope is not ReviewScope.FULL:
        partial_verdict_labels = {
            ReviewVerdict.APPROVED: ui.partial_approved_label,
            ReviewVerdict.CORRECTIONS_REQUIRED: ui.partial_corrections_required_label,
            ReviewVerdict.INCONCLUSIVE: ui.partial_inconclusive_label,
        }
        title = ui.partial_scope_title.format(
            verdict=partial_verdict_labels[publication.verdict],
            scope=scope_labels[publication.review_scope],
        )
    description = publication.summary
    if publication.review_scope is ReviewScope.TARGETED and publication.focus is not None:
        description = f"{ui.targeted_focus.format(focus=publication.focus)}\n\n{description}"
    primary = discord.Embed(
        title=f"{title} · {persona.identity.display_name}",
        description=description,
        color=colors[publication.verdict],
    )
    if publication.manual_review_reason:
        primary.add_field(
            name=ui.manual_review_title,
            value=publication.manual_review_reason[:1024],
            inline=False,
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
                    title=ui.continuation_title.format(display_name=persona.identity.display_name),
                    color=colors[publication.verdict],
                )
            )
        evidence = "\n" + ui.evidence_reference.format(evidence=finding.evidence) if finding.evidence else ""
        correction = ui.correction_instruction.format(recommendation=finding.recommendation)
        value = (f"{finding.detail}{evidence}\n{correction}")[:850]
        embeds[-1].add_field(
            name=f"{severity_icons[finding.severity]} {_capitalize_heading(finding.title)}"[:256],
            value=value,
            inline=False,
        )

    if publication.charges:
        alignment_labels = {
            "supported": ui.charge_supported,
            "unsupported": ui.charge_unsupported,
            "uncertain": ui.charge_uncertain,
            "not_found": ui.charge_not_found,
        }
        for group_start in range(0, len(publication.charges), 5):
            charge_embed = discord.Embed(
                title=(ui.charge_review_title if group_start == 0 else ui.charge_review_continuation_title),
                color=colors[publication.verdict],
            )
            for charge in publication.charges[group_start : group_start + 5]:
                charge_embed.add_field(
                    name=_capitalize_heading(charge.declared_charge),
                    value=f"**{alignment_labels[charge.alignment]}**\n{charge.note}",
                    inline=False,
                )
            embeds.append(charge_embed)
    return embeds[:10]


def _capitalize_heading(value: str) -> str:
    """Uppercase the first cased character without altering the remaining heading."""

    for index, character in enumerate(value):
        if character.lower() != character.upper():
            return f"{value[:index]}{character.upper()}{value[index + 1 :]}"
    return value


def _can_mention_role(
    channel: discord.TextChannel | discord.Thread,
    guild: discord.Guild,
    role: discord.Role,
) -> bool:
    if role.mentionable:
        return True
    bot_member = guild.me
    return bool(
        bot_member is not None
        and channel.permissions_for(bot_member).mention_everyone
    )


def _sanitize_role_notification_content(content: str | None) -> str | None:
    if content is None:
        return None
    rendered = content.strip()
    if not rendered or len(rendered) > MAX_ROLE_NOTIFICATION_CONTENT:
        raise PublicationError("Role notification content is invalid")
    rendered = re.sub(r"<@&[1-9][0-9]{16,19}>", "rol", rendered)
    rendered = re.sub(r"<@!?[1-9][0-9]{16,19}>", "usuario", rendered)
    return re.sub(
        r"@(everyone|here)\b",
        lambda match: f"@\u200b{match.group(1)}",
        rendered,
        flags=re.IGNORECASE,
    )


def _safe_thread_name(content: str, message_id: int, fallback_template: str) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    without_mentions = re.sub(r"<@!?\d+>", "", first_line).strip()
    normalized = " ".join(without_mentions.split())
    return normalized[:90] or fallback_template.format(message_id=message_id)


def _chunk_text(content: str, limit: int) -> list[str]:
    """Split a free-form response at readable boundaries within Discord's hard limit."""

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks
