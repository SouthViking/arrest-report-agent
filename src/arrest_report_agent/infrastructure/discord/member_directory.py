"""Read-only, guild-bound Discord member lookup for agent tools."""

from __future__ import annotations

import logging

import discord

from arrest_report_agent.application.ports import (
    GuildMemberLookupResult,
    GuildMemberProfile,
)
from arrest_report_agent.config.logging import exception_fields, log_event

AUTHOR_ROLE_LIMIT = 8
AUTHOR_ROLE_NAME_MAX_CHARS = 80
AUTHOR_ROLES_TOTAL_MAX_CHARS = 320
LOGGER = logging.getLogger(__name__)


class DiscordGuildMemberDirectory:
    """Resolve a bounded set of members inside the interaction's current guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot

    async def lookup_members(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
    ) -> GuildMemberLookupResult:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            log_event(
                LOGGER,
                "warning",
                "member_lookup_guild_unavailable",
                guild_id=guild_id,
                requested_members=len(user_ids),
            )
            return GuildMemberLookupResult(
                members={},
                not_found=frozenset(),
                unavailable=frozenset(user_ids),
            )

        members: dict[int, GuildMemberProfile] = {}
        not_found: set[int] = set()
        unavailable: set[int] = set()
        for user_id in user_ids:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    not_found.add(user_id)
                    continue
                except (discord.Forbidden, discord.HTTPException) as error:
                    unavailable.add(user_id)
                    log_event(
                        LOGGER,
                        "warning",
                        "member_lookup_failed",
                        exc_info=error,
                        guild_id=guild_id,
                        user_id=user_id,
                        **exception_fields(error),
                    )
                    continue

            members[user_id] = GuildMemberProfile(
                user_id=user_id,
                display_name=member.display_name,
                username=member.name,
                roles=tuple(compact_role_names(member)),
                joined_at=member.joined_at,
                created_at=member.created_at,
                is_bot=member.bot,
            )

        return GuildMemberLookupResult(
            members=members,
            not_found=frozenset(not_found),
            unavailable=frozenset(unavailable),
        )


def compact_role_names(member: discord.abc.User) -> list[str]:
    """Return hierarchy-ordered role names without IDs or integration roles."""

    candidates: list[tuple[int, int, str]] = []
    for role in getattr(member, "roles", ()):
        name = " ".join(str(getattr(role, "name", "")).split())
        is_default = getattr(role, "is_default", None)
        if (
            not name
            or name == "@everyone"
            or bool(getattr(role, "managed", False))
            or (callable(is_default) and is_default())
        ):
            continue
        candidates.append(
            (
                int(getattr(role, "position", 0)),
                int(getattr(role, "id", 0)),
                name[:AUTHOR_ROLE_NAME_MAX_CHARS],
            )
        )

    names: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for _, _, name in sorted(candidates, reverse=True):
        normalized = name.casefold()
        if normalized in seen:
            continue
        separator_chars = 2 if names else 0
        if total_chars + separator_chars + len(name) > AUTHOR_ROLES_TOTAL_MAX_CHARS:
            break
        seen.add(normalized)
        names.append(name)
        total_chars += separator_chars + len(name)
        if len(names) == AUTHOR_ROLE_LIMIT:
            break
    return names
