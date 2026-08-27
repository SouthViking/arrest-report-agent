"""Deterministic projections for connected Field Training Program members."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FtpMemberGroup(StrEnum):
    SUPERVISORY = "supervisory"
    MFTO = "mfto"
    FTO = "fto"
    FTT = "ftt"


@dataclass(frozen=True, slots=True)
class FtpPresenceMember:
    user_id: int
    display_name: str
    character_name: str
    group: FtpMemberGroup
    on_duty: bool


@dataclass(frozen=True, slots=True)
class FtpPresenceSnapshot:
    faction_id: int
    members: tuple[FtpPresenceMember, ...]
    ambiguous_connected_characters: int = 0

    def members_for(self, group: FtpMemberGroup) -> tuple[FtpPresenceMember, ...]:
        return tuple(member for member in self.members if member.group is group)

    @property
    def on_duty_count(self) -> int:
        return sum(member.on_duty for member in self.members)
