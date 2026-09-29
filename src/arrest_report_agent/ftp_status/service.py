"""Join cached SARP presence with configured Discord FTP roles."""

from __future__ import annotations

import logging
from typing import Protocol

from arrest_report_agent.application.member_names import load_member_profiles, unique_profile_match
from arrest_report_agent.application.ports import (
    GuildMemberProfile,
    GuildMemberSearchQuery,
    GuildMemberSearchResult,
)
from arrest_report_agent.config import FtpStatusOptions
from arrest_report_agent.config.logging import log_event
from arrest_report_agent.faction_directory import FactionDirectoryService

from .models import FtpMemberGroup, FtpPresenceMember, FtpPresenceSnapshot

LOGGER = logging.getLogger(__name__)


class FtpGuildMemberDirectory(Protocol):
    async def search_members(
        self,
        guild_id: int,
        query: GuildMemberSearchQuery,
    ) -> GuildMemberSearchResult: ...


class FtpStatusService:
    """Resolve connected faction characters to uniquely matching FTP members."""

    def __init__(
        self,
        faction_directory: FactionDirectoryService,
        member_directory: FtpGuildMemberDirectory,
    ) -> None:
        self.faction_directory = faction_directory
        self.member_directory = member_directory

    async def query(
        self,
        guild_id: int,
        options: FtpStatusOptions,
    ) -> FtpPresenceSnapshot:
        roster = await self.faction_directory.snapshot(options.faction_id)
        candidates = await self._ftp_members(guild_id, options.all_role_ids)
        connected = tuple(member for member in roster.members if member.online)

        selected: dict[int, FtpPresenceMember] = {}
        ambiguous = 0
        for character in connected:
            profile, is_ambiguous = unique_profile_match(character.name, candidates)
            if is_ambiguous:
                ambiguous += 1
            if profile is None:
                continue
            group = _member_group(profile, options)
            if group is None:
                continue
            resolved = FtpPresenceMember(
                user_id=profile.user_id,
                display_name=profile.display_name,
                character_name=character.name,
                group=group,
                on_duty=character.on_duty,
            )
            current = selected.get(profile.user_id)
            if current is None or (resolved.on_duty and not current.on_duty):
                selected[profile.user_id] = resolved

        members = tuple(
            sorted(
                selected.values(),
                key=lambda member: (
                    _group_order(member.group),
                    not member.on_duty,
                    member.display_name.casefold(),
                    member.user_id,
                ),
            )
        )
        if ambiguous:
            log_event(
                LOGGER,
                "warning",
                "ftp_connected_character_matches_ambiguous",
                guild_id=guild_id,
                faction_id=options.faction_id,
                ambiguous_connected_characters=ambiguous,
                ftp_candidates=len(candidates),
            )
        return FtpPresenceSnapshot(
            faction_id=options.faction_id,
            members=members,
            ambiguous_connected_characters=ambiguous,
        )

    async def _ftp_members(
        self,
        guild_id: int,
        role_ids: tuple[int, ...],
    ) -> tuple[GuildMemberProfile, ...]:
        return await load_member_profiles(self.member_directory, guild_id, role_ids=role_ids)


def _member_group(
    member: GuildMemberProfile,
    options: FtpStatusOptions,
) -> FtpMemberGroup | None:
    role_ids = set(member.role_ids)
    if role_ids.intersection(options.supervisory_role_ids):
        return FtpMemberGroup.SUPERVISORY
    if role_ids.intersection(options.mfto_role_ids):
        return FtpMemberGroup.MFTO
    if role_ids.intersection(options.fto_role_ids):
        return FtpMemberGroup.FTO
    if role_ids.intersection(options.ftt_role_ids):
        return FtpMemberGroup.FTT
    return None


def _group_order(group: FtpMemberGroup) -> int:
    return {
        FtpMemberGroup.SUPERVISORY: 0,
        FtpMemberGroup.MFTO: 1,
        FtpMemberGroup.FTO: 2,
        FtpMemberGroup.FTT: 3,
    }[group]
