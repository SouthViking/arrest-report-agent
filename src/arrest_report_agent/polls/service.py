"""Permission-aware native Discord poll operations."""

from __future__ import annotations

import builtins
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from arrest_report_agent.application.ports import PollCreateRequest, PollListQuery
from arrest_report_agent.config.logging import exception_fields, log_event

from .models import PollRecord
from .repository import PollRepository

LOGGER = logging.getLogger(__name__)


class DiscordPollService:
    """Use Discord as source of truth while keeping a minimal searchable index."""

    def __init__(self, bot: discord.Bot, repository: PollRepository) -> None:
        self.bot = bot
        self.repository = repository

    async def create(
        self,
        *,
        guild_id: int,
        current_channel_id: int,
        requester_user_id: int,
        target_channel_id: int | None,
        request: PollCreateRequest,
    ) -> dict[str, object]:
        channel_id = target_channel_id or current_channel_id
        resolved = await self._resolve_channel(
            guild_id=guild_id,
            channel_id=channel_id,
            requester_user_id=requester_user_id,
            require_send=True,
        )
        if isinstance(resolved, str):
            return _error(resolved)
        _, channel, _ = resolved
        answers = [
            discord.PollAnswer(answer.text, _poll_emoji(answer.emoji)) for answer in request.answers
        ]
        poll = discord.Poll(
            request.question,
            answers=answers,
            duration=request.duration_hours,
            allow_multiselect=request.allow_multiselect,
        )
        try:
            message = await channel.send(
                content=request.intro,
                poll=poll,
                silent=request.silent,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_create_forbidden",
                exc_info=error,
                guild_id=guild_id,
                channel_id=channel_id,
                **exception_fields(error),
            )
            return _error("forbidden")
        except discord.HTTPException as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_create_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=channel_id,
                **exception_fields(error),
            )
            return _error("temporarily_unavailable")

        now = message.created_at
        expires_at = (
            message.poll.expiry
            if message.poll is not None and message.poll.expiry is not None
            else now + timedelta(hours=request.duration_hours)
        )
        record = PollRecord(
            message_id=message.id,
            guild_id=guild_id,
            channel_id=channel.id,
            creator_user_id=requester_user_id,
            created_at=now,
            expires_at=expires_at,
        )
        try:
            await self.repository.create(record)
        except Exception as error:
            with suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
            log_event(
                LOGGER,
                "error",
                "agent_poll_metadata_create_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=channel.id,
                message_id=message.id,
                **exception_fields(error),
            )
            return _error("temporarily_unavailable")
        return {
            "ok": True,
            "message_id": str(message.id),
            "channel_id": str(channel.id),
            "message_url": message.jump_url,
            "expires_at": record.expires_at.isoformat(),
            "allow_multiselect": request.allow_multiselect,
            "answer_count": len(request.answers),
        }

    async def list(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        query: PollListQuery,
    ) -> dict[str, object]:
        try:
            records, total = await self.repository.list(guild_id, requester_user_id, query)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_list_failed",
                exc_info=error,
                guild_id=guild_id,
                **exception_fields(error),
            )
            return _error("temporarily_unavailable")

        visible: list[dict[str, object]] = []
        for record in records:
            resolved = await self._resolve_channel(
                guild_id=guild_id,
                channel_id=record.channel_id,
                requester_user_id=requester_user_id,
                require_send=False,
            )
            if isinstance(resolved, str):
                continue
            status = _record_status(record)
            visible.append(
                {
                    "message_id": str(record.message_id),
                    "channel_id": str(record.channel_id),
                    "creator_user_id": str(record.creator_user_id),
                    "created_at": record.created_at.isoformat(),
                    "expires_at": record.expires_at.isoformat(),
                    "status": status,
                    "message_url": _message_url(guild_id, record.channel_id, record.message_id),
                }
            )
        return {
            "ok": True,
            "polls": visible,
            "total_matching_metadata": total,
            "returned_visible": len(visible),
            "page": query.page,
            "limit": query.limit,
            "has_more": query.page * query.limit < total,
            "content_source": "metadata_index",
        }

    async def get(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        message_id: int,
        include_voters: bool,
        answer_ids: tuple[int, ...],
        voter_limit_per_answer: int,
        after_user_id: int | None,
    ) -> dict[str, object]:
        record, lookup_error = await self._safe_get_record(guild_id, message_id)
        if lookup_error is not None:
            return _error(lookup_error)
        if record is None:
            return _error("not_found")
        resolved = await self._resolve_channel(
            guild_id=guild_id,
            channel_id=record.channel_id,
            requester_user_id=requester_user_id,
            require_send=False,
        )
        if isinstance(resolved, str):
            return _error(resolved)
        _, channel, _ = resolved
        message = await self._fetch_message(record, channel)
        if isinstance(message, str):
            return _error(message)
        if message.poll is None:
            return _error("not_a_poll")

        poll = message.poll
        selected_ids = set(answer_ids)
        if selected_ids and any(poll.get_answer(answer_id) is None for answer_id in selected_ids):
            return _error("invalid_answer_id")
        total_votes = poll.total_votes() or 0
        total_selections = sum(answer.count or 0 for answer in poll.answers)
        answer_payloads: list[dict[str, object]] = []
        for answer in poll.answers:
            count = answer.count or 0
            payload: dict[str, object] = {
                "answer_id": answer.id,
                "text": answer.text,
                "emoji": _emoji_text(answer.emoji),
                "vote_count": count,
                "percentage": round(
                    count
                    / (total_selections if poll.allow_multiselect else total_votes)
                    * 100,
                    2,
                )
                if (total_selections if poll.allow_multiselect else total_votes)
                else 0.0,
                "percentage_basis": (
                    "all_selections" if poll.allow_multiselect else "participants"
                ),
            }
            if include_voters and (not selected_ids or answer.id in selected_ids):
                try:
                    voters, next_after = await self._fetch_voters(
                        answer,
                        limit=voter_limit_per_answer,
                        after_user_id=after_user_id,
                    )
                    payload["voters"] = voters
                    payload["next_after_user_id"] = next_after
                except discord.Forbidden:
                    payload["voters_error"] = "forbidden"
                except discord.HTTPException:
                    payload["voters_error"] = "temporarily_unavailable"
            answer_payloads.append(payload)

        ended = bool(poll.has_ended()) or datetime.now(UTC) >= record.expires_at
        now = datetime.now(UTC)
        try:
            if ended and record.ended_at is None:
                await self.repository.mark_ended(guild_id, message_id, at=now)
            else:
                await self.repository.mark_synced(guild_id, message_id, at=now)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_sync_metadata_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=record.channel_id,
                message_id=message_id,
                **exception_fields(error),
            )
        return {
            "ok": True,
            "poll": {
                "message_id": str(message.id),
                "channel_id": str(record.channel_id),
                "message_url": message.jump_url,
                "question": poll.question.text,
                "allow_multiselect": bool(poll.allow_multiselect),
                "status": "ended" if ended else "open",
                "created_at": record.created_at.isoformat(),
                "expires_at": (poll.expiry or record.expires_at).isoformat(),
                "total_participants": None if poll.allow_multiselect else total_votes,
                "total_votes": total_votes,
                "total_selections": total_selections,
                "results_finalized": bool(poll.results and poll.results.is_finalized),
                "answers": answer_payloads,
            },
            "content_source": "discord_live",
        }

    async def end(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        message_id: int,
    ) -> dict[str, object]:
        record, lookup_error = await self._safe_get_record(guild_id, message_id)
        if lookup_error is not None:
            return _error(lookup_error)
        if record is None:
            return _error("not_found")
        if record.creator_user_id != requester_user_id:
            return _error("not_creator")
        resolved = await self._resolve_channel(
            guild_id=guild_id,
            channel_id=record.channel_id,
            requester_user_id=requester_user_id,
            require_send=False,
        )
        if isinstance(resolved, str):
            return _error(resolved)
        _, channel, _ = resolved
        message = await self._fetch_message(record, channel)
        if isinstance(message, str):
            return _error(message)
        if message.poll is None:
            return _error("not_a_poll")
        if message.poll.has_ended():
            return {"ok": True, "already_ended": True}
        try:
            ended_message = await message.end_poll()
        except discord.Forbidden as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_end_forbidden",
                exc_info=error,
                guild_id=guild_id,
                channel_id=record.channel_id,
                message_id=message_id,
                **exception_fields(error),
            )
            return _error("forbidden")
        except discord.HTTPException as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_end_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=record.channel_id,
                message_id=message_id,
                **exception_fields(error),
            )
            return _error("temporarily_unavailable")
        ended_at = datetime.now(UTC)
        metadata_updated = True
        try:
            await self.repository.mark_ended(guild_id, message_id, at=ended_at)
        except Exception as error:
            metadata_updated = False
            log_event(
                LOGGER,
                "warning",
                "agent_poll_end_metadata_update_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=record.channel_id,
                message_id=message_id,
                **exception_fields(error),
            )
        return {
            "ok": True,
            "message_id": str(ended_message.id),
            "message_url": ended_message.jump_url,
            "ended_at": ended_at.isoformat(),
            "metadata_updated": metadata_updated,
        }

    async def _safe_get_record(
        self,
        guild_id: int,
        message_id: int,
    ) -> tuple[PollRecord | None, str | None]:
        try:
            return await self.repository.get(guild_id, message_id), None
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_metadata_lookup_failed",
                exc_info=error,
                guild_id=guild_id,
                message_id=message_id,
                **exception_fields(error),
            )
            return None, "temporarily_unavailable"

    async def _resolve_channel(
        self,
        *,
        guild_id: int,
        channel_id: int,
        requester_user_id: int,
        require_send: bool,
    ) -> tuple[discord.Guild, discord.TextChannel | discord.Thread, discord.Member] | str:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "guild_unavailable"
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                return "channel_not_found"
            except discord.Forbidden:
                return "forbidden"
            except discord.HTTPException:
                return "temporarily_unavailable"
            if not isinstance(fetched, (discord.TextChannel, discord.Thread)):
                return "unsupported_channel"
            channel = fetched
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return "unsupported_channel"
        requester = guild.get_member(requester_user_id)
        if requester is None:
            try:
                requester = await guild.fetch_member(requester_user_id)
            except discord.NotFound:
                return "requester_not_found"
            except discord.Forbidden:
                return "forbidden"
            except discord.HTTPException:
                return "temporarily_unavailable"
        bot_member = guild.me
        requester_permissions = channel.permissions_for(requester)
        bot_permissions = channel.permissions_for(bot_member)
        if not requester_permissions.view_channel or not bot_permissions.view_channel:
            return "forbidden"
        if not requester_permissions.read_message_history:
            return "requester_missing_read_history"
        if not bot_permissions.read_message_history:
            return "bot_missing_read_history"
        if require_send:
            requester_can_send = (
                requester_permissions.send_messages_in_threads
                if isinstance(channel, discord.Thread)
                else requester_permissions.send_messages
            )
            bot_can_send = (
                bot_permissions.send_messages_in_threads
                if isinstance(channel, discord.Thread)
                else bot_permissions.send_messages
            )
            if not requester_can_send or not requester_permissions.send_polls:
                return "requester_missing_poll_permission"
            if not bot_can_send or not bot_permissions.send_polls:
                return "bot_missing_poll_permission"
        return guild, channel, requester

    async def _fetch_message(
        self,
        record: PollRecord,
        channel: discord.TextChannel | discord.Thread,
    ) -> discord.Message | str:
        try:
            return await channel.fetch_message(record.message_id)
        except discord.NotFound:
            with suppress(Exception):
                await self.repository.mark_deleted(
                    record.guild_id,
                    record.message_id,
                    at=datetime.now(UTC),
                )
            return "deleted"
        except discord.Forbidden:
            return "forbidden"
        except discord.HTTPException:
            return "temporarily_unavailable"

    @staticmethod
    async def _fetch_voters(
        answer: discord.PollAnswer,
        *,
        limit: int,
        after_user_id: int | None,
    ) -> tuple[builtins.list[dict[str, object]], str | None]:
        after = discord.Object(id=after_user_id) if after_user_id is not None else None
        users: builtins.list[Any] = []
        async for user in answer.voters(limit=limit + 1, after=after):
            users.append(user)
        has_more = len(users) > limit
        selected = users[:limit]
        voters = [
            {
                "user_id": str(user.id),
                "display_name": getattr(user, "display_name", user.name),
                "username": user.name,
            }
            for user in selected
        ]
        return voters, str(selected[-1].id) if has_more and selected else None


def _error(code: str) -> dict[str, object]:
    return {"ok": False, "error": code}


def _poll_emoji(value: str | None) -> discord.PartialEmoji | str | None:
    if value is None:
        return None
    if value.startswith("<") and value.endswith(">"):
        return discord.PartialEmoji.from_str(value)
    return value


def _emoji_text(value: object | None) -> str | None:
    return None if value is None else str(value)


def _record_status(record: PollRecord) -> str:
    if record.deleted_at is not None:
        return "deleted"
    if record.ended_at is not None or record.expires_at <= datetime.now(UTC):
        return "ended"
    return "open"


def _message_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
