"""Verify staff authority from Discord and publish manual emergency approvals without an LLM."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from arrest_report_agent.application.approvals import (
    StaffApproval,
    invalidates_approval_context,
    is_approval_confirmation,
    is_explicit_approval,
)
from arrest_report_agent.application.conversations import ConversationKey
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import (
    ConversationMessage,
    InteractionMode,
    ReviewCase,
    ReviewPublication,
    ReviewVerdict,
)
from arrest_report_agent.domain.errors import ContextUnavailableError
from arrest_report_agent.review_analytics import RecordingReviewPublisher

from .publisher import DiscordReviewPublisher

if TYPE_CHECKING:
    from arrest_report_agent.application import ReviewRunContext
    from arrest_report_agent.config import AppSettings, GuildRuntimeSettings

    from .cog import ReviewCog
    from .context import ReviewTarget
    from .trigger import DiscordAccessPolicy

LOGGER = logging.getLogger(__name__)


def staff_role_ids(settings: AppSettings | GuildRuntimeSettings) -> tuple[int, ...]:
    options = getattr(settings, "arrest_report_lookup_options", None)
    return options.approval_override_role_ids if options is not None else ()


async def authorized_staff(
    guild: discord.Guild,
    user_id: int,
    settings: AppSettings | GuildRuntimeSettings,
    policy: DiscordAccessPolicy,
) -> discord.Member | None:
    roles = staff_role_ids(settings)
    if not roles or not policy.guilds.allows(guild.id):
        return None
    # Fetch current membership: a cached name, admin permission or user allowlist is insufficient.
    member = await guild.fetch_member(user_id)
    if member.bot or policy.blocks_member(member):
        return None
    return member if any(role.id in roles for role in member.roles) else None


async def authorize_message(
    run: ReviewRunContext,
    target: ReviewTarget,
    settings: AppSettings | GuildRuntimeSettings,
    policy: DiscordAccessPolicy,
    message_id: int,
) -> StaffApproval | None:
    current_ids = {run.case.trigger_message_id}
    if run.live_conversation is not None:
        current_ids.update(run.live_conversation.message_ids)

    def deny(reason: str) -> None:
        log_event(
            LOGGER,
            "info",
            "staff_approval_denied",
            reason=reason,
            guild_id=run.case.guild_id,
            root_message_id=run.case.root_message_id,
            requested_message_id=message_id,
            current_message_id=max(current_ids),
        )

    # Never reuse historical authorization or ignore a newer correction/revocation.
    if message_id != max(current_ids):
        return deny("message_not_current")
    channels = [target.trigger.channel]
    if run.case.thread_id is not None and run.case.thread_id != target.trigger.channel.id:
        channel = await target.root.guild.fetch_channel(run.case.thread_id)
        if isinstance(channel, discord.Thread):
            channels.insert(0, channel)
    for channel in channels:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        if (
            message.guild is None
            or message.guild.id != run.case.guild_id
            or message.author.bot
            or message.webhook_id is not None
        ):
            return deny("invalid_message_origin")
        if message.channel.id != run.case.thread_id and message.id != run.case.trigger_message_id:
            # A parent-channel inbox may also receive replies about a different report.
            reference = getattr(message, "reference", None)
            if getattr(reference, "message_id", None) not in {
                run.case.root_message_id,
                run.case.trigger_message_id,
            }:
                return deny("different_report_reference")
        explicit = is_explicit_approval(message.content)
        if not explicit and not is_approval_confirmation(message.content):
            return deny("not_an_approval_instruction_or_confirmation")
        member = await authorized_staff(message.guild, message.author.id, settings, policy)
        if member is None:
            return deny("missing_staff_role_or_blocked_member")
        if explicit:
            return StaffApproval(member.id, message.id, message.content[:500])
        # Confirmations may reference an earlier instruction only inside the same report thread.
        if message.channel.id != run.case.thread_id:
            return deny("confirmation_requires_report_thread")
        visible_ids = {item.id for item in run.case.messages}
        visible_ids.update(current_ids)
        async for earlier in message.channel.history(limit=50, before=message, oldest_first=False):
            if earlier.author.id != member.id:
                continue
            if earlier.id not in visible_ids:
                return deny("instruction_outside_available_context")
            if (
                earlier.guild is None
                or earlier.guild.id != run.case.guild_id
                or earlier.channel.id != message.channel.id
                or earlier.author.bot
                or earlier.webhook_id is not None
            ):
                return deny("invalid_instruction_origin")
            if is_explicit_approval(earlier.content):
                return StaffApproval(
                    member.id,
                    message.id,
                    f"Orden previa: {earlier.content[:300]} · Ratificación actual: {message.content[:150]}",
                    instruction_message_id=earlier.id,
                )
            if not is_approval_confirmation(earlier.content) and invalidates_approval_context(
                earlier.content
            ):
                return deny("prior_instruction_revoked_conditional_or_changed")
        return deny("prior_approval_instruction_not_found")
    return deny("current_message_not_found")


async def open_manual_approval(cog: ReviewCog, ctx: discord.ApplicationContext) -> None:
    """Open the form without consuming the interaction's response with a defer."""

    if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
        await ctx.respond("Usa /aprobar-arresto dentro del hilo del reporte.", ephemeral=True)
        return
    roles = staff_role_ids(cog.settings)
    # Open immediately using interaction metadata; fetch current membership again on submission.
    if (
        not cog.access_policy.guilds.allows(ctx.guild.id)
        or cog.access_policy.blocks_member(ctx.author)
        or ctx.author.bot
        or not any(role.id in roles for role in getattr(ctx.author, "roles", ()))
    ):
        await ctx.respond("Solo Command Staff y Executive Staff pueden aprobar manualmente.", ephemeral=True)
        return
    await ctx.send_modal(
        ManualArrestApprovalModal(
            cog,
            requester_id=ctx.author.id,
            guild_id=ctx.guild.id,
            thread_id=ctx.channel.id,
        )
    )


class ManualArrestApprovalModal(discord.ui.DesignerModal):
    def __init__(self, cog: ReviewCog, *, requester_id: int, guild_id: int, thread_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.reason_input = discord.ui.InputText(
            custom_id="manual-arrest-approval:reason",
            style=discord.InputTextStyle.long,
            placeholder="Explica por qué autorizas la aprobación manual de este reporte.",
            required=True,
            min_length=5,
            max_length=500,
        )
        super().__init__(
            discord.ui.Label("Motivo de la aprobación", self.reason_input),
            title="Aprobar reporte de arresto",
            custom_id=f"manual-arrest-approval:{guild_id}:{thread_id}:{requester_id}",
            timeout=300,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.requester_id
            or interaction.guild is None
            or interaction.guild.id != self.guild_id
            or interaction.channel_id != self.thread_id
        ):
            await interaction.response.send_message(
                "Este formulario pertenece al solicitante y al hilo originales.",
                ephemeral=True,
            )
            return
        ctx = discord.ApplicationContext(self.cog.bot, interaction)
        try:
            await ctx.defer(ephemeral=True, invisible=False)
            # Modal submissions are separate interactions: reload configuration and verify roles again.
            async with self.cog._bind_guild_runtime(self.guild_id):
                await approve_manually(
                    self.cog,
                    ctx,
                    self.reason_input.value or "",
                    response_deferred=True,
                )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "manual_arrest_approval_modal_failed",
                guild_id=self.guild_id,
                thread_id=self.thread_id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await ctx.respond(
                "No pude completar la aprobación manual. Revisa el estado del reporte antes de reintentar.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def approve_manually(
    cog: ReviewCog,
    ctx: discord.ApplicationContext,
    reason: str,
    *,
    response_deferred: bool = False,
) -> None:
    """Use the normal recorded publisher; the reason and actor remain in the public review."""

    async def reply(text: str, *, followup: bool = False) -> None:
        send = ctx.send_followup if followup else ctx.respond
        await send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    if ctx.guild is None or not isinstance(ctx.channel, discord.Thread):
        await reply("Usa /aprobar-arresto dentro del hilo del reporte.")
        return
    reason = reason.strip()
    if not 5 <= len(reason) <= 500:
        await reply("Indica un motivo de aprobación de entre 5 y 500 caracteres.")
        return
    if not response_deferred:
        await ctx.defer(ephemeral=True)
    try:
        member = await authorized_staff(ctx.guild, ctx.author.id, cog.settings, cog.access_policy)
    except (discord.HTTPException, discord.InvalidData):
        await reply("No pude verificar tus roles en Discord. No se aprobó el reporte.", followup=True)
        return
    if member is None:
        await reply("Solo Command Staff y Executive Staff pueden aprobar manualmente.", followup=True)
        return
    if cog.report_review_repository is None:
        await reply("No está disponible el registro de auditoría. No se aprobó el reporte.", followup=True)
        return
    key = ConversationKey(ctx.guild.id, ctx.channel.id)
    lease = await cog._conversations.acquire(key, ctx.interaction.id)
    if lease is None:
        await reply(
            "Hay una revisión en curso en este hilo. Espera a que termine y vuelve a intentarlo.",
            followup=True,
        )
        return
    try:
        target = await cog.context_builder.manual_approval_target(ctx.channel, member)
        root = target.root
        case = ReviewCase(
            guild_id=ctx.guild.id,
            parent_channel_id=target.parent.id,
            channel_id=ctx.channel.id,
            thread_id=ctx.channel.id,
            root_message_id=root.id,
            trigger_message_id=ctx.interaction.id,
            requester_user_id=member.id,
            requester_display_name=member.display_name,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            structured_submission_id=target.structured_submission_id,
            structured_report_id=target.structured_report_id,
            messages=[
                ConversationMessage(
                    id=root.id,
                    author_id=root.author.id,
                    author_display=root.author.display_name,
                    author_is_bot=root.author.bot,
                    content=root.content,
                    created_at=root.created_at,
                )
            ],
            attachments=[],
        )
        approval = StaffApproval(member.id, ctx.interaction.id, reason, source="command")
        publication = ReviewPublication(
            verdict=ReviewVerdict.APPROVED,
            summary="Reporte aprobado manualmente por Command/Executive Staff.",
            findings=[
                approval.finding(
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                    detail="Aprobación humana de emergencia, sin evaluación del agente "
                    "ni verificación de SARP. "
                    "No confirma el cierre de órdenes de arresto.",
                )
            ],
        )
        publisher = RecordingReviewPublisher(
            DiscordReviewPublisher(cog.bot, cog.settings, target, cog.persona),
            cog.report_review_repository,
            case,
            model="manual",
            reasoning_effort="none",
            persona_id=cog.persona.id,
            persona_revision=cog.persona.revision,
            app_version=cog.settings.app_version,
        )
        await publisher.publish(publication)
        log_event(
            LOGGER,
            "info",
            "arrest_report_manually_approved",
            guild_id=ctx.guild.id,
            root_message_id=root.id,
            thread_id=ctx.channel.id,
            authorized_by_user_id=member.id,
            interaction_id=ctx.interaction.id,
            reason=reason,
        )
        await reply("Aprobación manual publicada y registrada en el historial del reporte.", followup=True)
    except ContextUnavailableError:
        await reply("Este hilo no corresponde a un reporte de arresto habilitado.", followup=True)
    except Exception as error:
        log_event(
            LOGGER,
            "error",
            "manual_arrest_approval_failed",
            guild_id=ctx.guild.id,
            thread_id=ctx.channel.id,
            interaction_id=ctx.interaction.id,
            **exception_fields(error),
        )
        await reply(
            "No pude completar la aprobación manual. Revisa el estado del reporte antes de reintentar.",
            followup=True,
        )
    finally:
        await cog._conversations.release(lease)
