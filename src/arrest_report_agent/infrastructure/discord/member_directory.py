"""Read-only, guild-bound Discord member and role discovery for agent tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime

import discord

from arrest_report_agent.application.ports import (
    ChannelMemberSearchQuery,
    GuildMemberLookupResult,
    GuildMemberNameMatchStrategy,
    GuildMemberProfile,
    GuildMemberSearchQuery,
    GuildMemberSearchResult,
    GuildRoleProfile,
    GuildRoleSearchQuery,
    GuildRoleSearchResult,
)
from arrest_report_agent.config.logging import exception_fields, log_event

AUTHOR_ROLE_LIMIT = 8
AUTHOR_ROLE_NAME_MAX_CHARS = 80
AUTHOR_ROLES_TOTAL_MAX_CHARS = 320
CURSOR_VERSION = 1
LOGGER = logging.getLogger(__name__)


class DiscordGuildMemberDirectory:
    """Discover bounded member data without ever selecting another guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._chunk_locks: dict[int, asyncio.Lock] = {}

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

            members[user_id] = _member_profile(member)

        return GuildMemberLookupResult(
            members=members,
            not_found=frozenset(not_found),
            unavailable=frozenset(unavailable),
        )

    async def search_roles(
        self,
        guild_id: int,
        query: GuildRoleSearchQuery,
    ) -> GuildRoleSearchResult:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return GuildRoleSearchResult(roles=(), error="guild_unavailable")

        members: tuple[discord.Member, ...] = ()
        if query.include_member_count:
            members, error = await self._complete_members(guild)
            if error is not None:
                return GuildRoleSearchResult(roles=(), error=error)

        selected_ids = set(query.role_ids)
        normalized_query = _normalize(query.query or "")
        roles: list[GuildRoleProfile] = []
        for role in guild.roles:
            if _is_default_role(role):
                continue
            if selected_ids and role.id not in selected_ids:
                continue
            if not query.include_managed and role.managed:
                continue
            if normalized_query and normalized_query not in _normalize(role.name):
                continue
            roles.append(
                GuildRoleProfile(
                    role_id=role.id,
                    name=role.name[:AUTHOR_ROLE_NAME_MAX_CHARS],
                    position=role.position,
                    managed=role.managed,
                    member_count=sum(1 for member in members if role.id in _assigned_role_ids(member)),
                )
            )

        roles = _sort_roles(roles, query.sort_by, query.sort_direction)
        fingerprint = _role_query_fingerprint(query)
        page, next_cursor, cursor_error = _paginate(
            roles,
            query.limit,
            query.cursor,
            kind="roles",
            fingerprint=fingerprint,
        )
        if cursor_error is not None:
            return GuildRoleSearchResult(roles=(), error=cursor_error)
        return GuildRoleSearchResult(roles=tuple(page), next_cursor=next_cursor)

    async def search_members(
        self,
        guild_id: int,
        query: GuildMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return GuildMemberSearchResult(members=(), error="guild_unavailable")
        members, error = await self._complete_members(guild)
        if error is not None:
            return GuildMemberSearchResult(members=(), error=error)
        return _search_member_candidates(members, query, cursor_kind="guild_members")

    async def search_channel_members(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: ChannelMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return GuildMemberSearchResult(members=(), error="guild_unavailable")

        requester = guild.get_member(requester_user_id)
        if requester is None:
            try:
                requester = await guild.fetch_member(requester_user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                return self._channel_error(
                    "requester_unavailable",
                    guild_id,
                    query.channel_id,
                    error,
                )

        channel = guild.get_channel_or_thread(query.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(query.channel_id)
            except discord.InvalidData as error:
                return self._channel_error("channel_outside_guild", guild_id, query.channel_id, error)
            except discord.NotFound as error:
                return self._channel_error("channel_not_found", guild_id, query.channel_id, error)
            except (discord.Forbidden, discord.HTTPException) as error:
                return self._channel_error("channel_unavailable", guild_id, query.channel_id, error)

        if channel.guild.id != guild_id:
            return self._channel_error("channel_outside_guild", guild_id, query.channel_id)
        if not _supported_channel(channel, query.membership):
            return self._channel_error("unsupported_channel", guild_id, query.channel_id)

        requester_permissions = channel.permissions_for(requester)
        if not requester_permissions.view_channel:
            return self._channel_error("channel_forbidden", guild_id, query.channel_id)
        if query.membership == "recent_participants" and not (requester_permissions.read_message_history):
            return self._channel_error("channel_forbidden", guild_id, query.channel_id)
        if isinstance(channel, discord.Thread) and channel.is_private() and channel.id != current_channel_id:
            return self._channel_error("private_thread_forbidden", guild_id, query.channel_id)

        members, error = await self._complete_members(guild)
        if error is not None:
            return GuildMemberSearchResult(members=(), error=error)
        by_id = {member.id: member for member in members}

        try:
            candidates = await _channel_candidates(
                channel,
                query.membership,
                query.message_limit,
                members,
                by_id,
            )
        except discord.Forbidden as error:
            return self._channel_error("channel_forbidden", guild_id, query.channel_id, error)
        except discord.HTTPException as error:
            return self._channel_error("channel_unavailable", guild_id, query.channel_id, error)

        return _search_member_candidates(
            candidates,
            query.members,
            cursor_kind=(f"channel_members:{query.channel_id}:{query.membership}:{query.message_limit}"),
        )

    async def _complete_members(
        self,
        guild: discord.Guild,
    ) -> tuple[tuple[discord.Member, ...], str | None]:
        if bool(getattr(guild, "chunked", True)):
            return tuple(guild.members), None

        lock = self._chunk_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if not bool(getattr(guild, "chunked", True)):
                try:
                    await guild.chunk(cache=True)
                except (TimeoutError, discord.ClientException, discord.HTTPException) as error:
                    log_event(
                        LOGGER,
                        "warning",
                        "member_directory_chunk_failed",
                        exc_info=error,
                        guild_id=guild.id,
                        **exception_fields(error),
                    )
                    return (), "member_directory_unavailable"
        return tuple(guild.members), None

    def _channel_error(
        self,
        code: str,
        guild_id: int,
        channel_id: int,
        error: Exception | None = None,
    ) -> GuildMemberSearchResult:
        log_event(
            LOGGER,
            "warning",
            "channel_member_search_failed",
            exc_info=error,
            guild_id=guild_id,
            channel_id=channel_id,
            lookup_error=code,
            **(exception_fields(error) if error is not None else {}),
        )
        return GuildMemberSearchResult(members=(), error=code)


async def _channel_candidates(
    channel: object,
    membership: str,
    message_limit: int,
    all_members: tuple[discord.Member, ...],
    members_by_id: dict[int, discord.Member],
) -> tuple[discord.Member, ...]:
    if membership == "can_view":
        return tuple(
            member
            for member in all_members
            if channel.permissions_for(member).view_channel  # type: ignore[attr-defined]
        )
    if membership == "voice_connected":
        return tuple(channel.members)  # type: ignore[attr-defined]
    if membership == "thread_members":
        thread_members = await channel.fetch_members()  # type: ignore[attr-defined]
        return tuple(
            member
            for thread_member in thread_members
            if (member := members_by_id.get(thread_member.id)) is not None
        )

    seen: dict[int, discord.Member] = {}
    async for message in channel.history(  # type: ignore[attr-defined]
        limit=message_limit,
        oldest_first=False,
    ):
        member = members_by_id.get(message.author.id)
        if member is not None:
            seen.setdefault(member.id, member)
    return tuple(seen.values())


def _supported_channel(channel: object, membership: str) -> bool:
    if membership == "recent_participants":
        return isinstance(channel, (discord.TextChannel, discord.Thread))
    if membership == "thread_members":
        return isinstance(channel, discord.Thread)
    if membership == "voice_connected":
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
    return isinstance(
        channel,
        (
            discord.TextChannel,
            discord.Thread,
            discord.ForumChannel,
            discord.VoiceChannel,
            discord.StageChannel,
        ),
    )


def _search_member_candidates(
    candidates: Iterable[discord.Member],
    query: GuildMemberSearchQuery,
    *,
    cursor_kind: str,
) -> GuildMemberSearchResult:
    if _invalid_member_filters(query):
        return GuildMemberSearchResult(members=(), error="invalid_member_filters")
    eligible = [member for member in candidates if _matches_member_filters(member, query)]
    selected, name_match_strategy = _select_name_matches(eligible, query.query)
    selected = _sort_members(selected, query.sort_by, query.sort_direction)
    fingerprint = _member_query_fingerprint(query)
    page, next_cursor, cursor_error = _paginate(
        selected,
        query.limit,
        query.cursor,
        kind=cursor_kind,
        fingerprint=fingerprint,
    )
    if cursor_error is not None:
        return GuildMemberSearchResult(members=(), error=cursor_error)
    return GuildMemberSearchResult(
        members=tuple(_member_profile(member) for member in page),
        next_cursor=next_cursor,
        name_match_strategy=name_match_strategy,
        candidate_count=len(selected) if query.query else None,
    )


def _invalid_member_filters(query: GuildMemberSearchQuery) -> bool:
    excluded = set(query.role_ids_none)
    if excluded.intersection(query.role_ids_any) or excluded.intersection(query.role_ids_all):
        return True
    return bool(
        query.joined_after is not None
        and query.joined_before is not None
        and query.joined_after > query.joined_before
    )


def _matches_member_filters(member: discord.Member, query: GuildMemberSearchQuery) -> bool:
    if query.user_ids and member.id not in set(query.user_ids):
        return False
    if query.is_bot is not None and member.bot is not query.is_bot:
        return False

    member_role_ids = _assigned_role_ids(member)
    if query.role_ids_any and not member_role_ids.intersection(query.role_ids_any):
        return False
    if query.role_ids_all and not set(query.role_ids_all).issubset(member_role_ids):
        return False
    if query.role_ids_none and member_role_ids.intersection(query.role_ids_none):
        return False
    if query.joined_after is not None and (member.joined_at is None or member.joined_at < query.joined_after):
        return False
    return not (
        query.joined_before is not None
        and (member.joined_at is None or member.joined_at > query.joined_before)
    )


def _select_name_matches(
    members: list[discord.Member],
    query: str | None,
) -> tuple[list[discord.Member], GuildMemberNameMatchStrategy | None]:
    normalized_query = _normalize(query or "")
    if not normalized_query:
        return members, None

    exact = [member for member in members if _matches_all_terms(member, normalized_query)]
    if exact:
        return exact, "exact_terms"

    query_tokens = _name_tokens(normalized_query)
    if len(query_tokens) < 2:
        return [], None

    initial_and_surname = [
        member for member in members if _matches_initial_and_surname(member, query_tokens)
    ]
    if initial_and_surname:
        return initial_and_surname, "initial_and_surname"

    surname = query_tokens[-1]
    surname_matches = [member for member in members if _matches_surname(member, surname)]
    if surname_matches:
        return surname_matches, "surname"
    return [], None


def _matches_all_terms(member: discord.Member, normalized_query: str) -> bool:
    terms = normalized_query.split()
    return any(all(term in alias for term in terms) for alias in _member_name_aliases(member))


def _matches_initial_and_surname(
    member: discord.Member,
    query_tokens: tuple[str, ...],
) -> bool:
    expected_initial = query_tokens[0][0]
    expected_surname = query_tokens[-1]
    return any(
        len(alias) >= 2
        and len(alias[0]) == 1
        and alias[0] == expected_initial
        and alias[-1] == expected_surname
        for alias in (_name_tokens(value) for value in _member_name_aliases(member))
    )


def _matches_surname(member: discord.Member, expected_surname: str) -> bool:
    return any(
        bool(alias) and alias[-1] == expected_surname
        for alias in (_name_tokens(value) for value in _member_name_aliases(member))
    )


def _member_name_aliases(member: discord.Member) -> tuple[str, ...]:
    aliases: list[str] = []
    for source in (member.display_name, member.name):
        aliases.extend(_normalize(value) for value in re.split(r"[/|]", source) if value.strip())
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def _sort_members(
    members: list[discord.Member],
    sort_by: str,
    sort_direction: str,
) -> list[discord.Member]:
    minimum = datetime.min.replace(tzinfo=UTC)
    if sort_by == "joined_at":

        def key(member: discord.Member) -> tuple[object, int]:
            return member.joined_at or minimum, member.id
    elif sort_by == "created_at":

        def key(member: discord.Member) -> tuple[object, int]:
            return member.created_at, member.id
    else:

        def key(member: discord.Member) -> tuple[object, int]:
            return _normalize(member.display_name), member.id

    return sorted(members, key=key, reverse=sort_direction == "desc")


def _sort_roles(
    roles: list[GuildRoleProfile],
    sort_by: str,
    sort_direction: str,
) -> list[GuildRoleProfile]:
    if sort_by == "name":

        def key(role: GuildRoleProfile) -> tuple[object, int]:
            return _normalize(role.name), role.role_id
    elif sort_by == "member_count":

        def key(role: GuildRoleProfile) -> tuple[object, int]:
            return role.member_count, role.role_id
    else:

        def key(role: GuildRoleProfile) -> tuple[object, int]:
            return role.position, role.role_id

    return sorted(roles, key=key, reverse=sort_direction == "desc")


def _paginate[T](
    items: list[T],
    limit: int,
    cursor: str | None,
    *,
    kind: str,
    fingerprint: str,
) -> tuple[list[T], str | None, str | None]:
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if (
            decoded is None
            or decoded.get("v") != CURSOR_VERSION
            or decoded.get("kind") != kind
            or decoded.get("fingerprint") != fingerprint
            or not isinstance(decoded.get("offset"), int)
            or int(decoded["offset"]) < 0
        ):
            return [], None, "invalid_cursor"
        offset = int(decoded["offset"])

    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _encode_cursor(kind, fingerprint, next_offset) if next_offset < len(items) else None
    return page, next_cursor, None


def _encode_cursor(kind: str, fingerprint: str, offset: int) -> str:
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "kind": kind,
            "fingerprint": fingerprint,
            "offset": offset,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, object] | None:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _member_query_fingerprint(query: GuildMemberSearchQuery) -> str:
    return _fingerprint(
        {
            "query": query.query,
            "user_ids": query.user_ids,
            "role_ids_any": query.role_ids_any,
            "role_ids_all": query.role_ids_all,
            "role_ids_none": query.role_ids_none,
            "is_bot": query.is_bot,
            "joined_after": _iso(query.joined_after),
            "joined_before": _iso(query.joined_before),
            "sort_by": query.sort_by,
            "sort_direction": query.sort_direction,
        }
    )


def _role_query_fingerprint(query: GuildRoleSearchQuery) -> str:
    return _fingerprint(
        {
            "query": query.query,
            "role_ids": query.role_ids,
            "include_managed": query.include_managed,
            "sort_by": query.sort_by,
            "sort_direction": query.sort_direction,
        }
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def _member_profile(member: discord.Member) -> GuildMemberProfile:
    return GuildMemberProfile(
        user_id=member.id,
        display_name=member.display_name,
        username=member.name,
        roles=tuple(compact_role_names(member)),
        joined_at=member.joined_at,
        created_at=member.created_at,
        is_bot=member.bot,
        role_ids=tuple(sorted(_member_role_ids(member))),
    )


def _member_role_ids(member: object) -> set[int]:
    return {
        int(role.id)
        for role in getattr(member, "roles", ())
        if not _is_default_role(role) and not bool(getattr(role, "managed", False))
    }


def _assigned_role_ids(member: object) -> set[int]:
    return {int(role.id) for role in getattr(member, "roles", ()) if not _is_default_role(role)}


def _is_default_role(role: object) -> bool:
    is_default = getattr(role, "is_default", None)
    return bool(callable(is_default) and is_default()) or getattr(role, "name", "") == "@everyone"


def compact_role_names(member: discord.abc.User) -> list[str]:
    """Return hierarchy-ordered role names without IDs or integration roles."""

    candidates: list[tuple[int, int, str]] = []
    for role in getattr(member, "roles", ()):
        name = " ".join(str(getattr(role, "name", "")).split())
        if not name or _is_default_role(role) or bool(getattr(role, "managed", False)):
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
