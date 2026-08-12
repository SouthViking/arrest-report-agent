"""PostgreSQL repository for reusable operational member profiles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import guild_member_profiles

from .models import MemberProfile


class MemberProfileRepository:
    """Keep member metadata persistence outside Discord and pager adapters."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get(self, guild_id: int, user_id: int) -> MemberProfile | None:
        row = await self.client.fetch_one(
            sa.select(guild_member_profiles).where(
                guild_member_profiles.c.guild_id == guild_id,
                guild_member_profiles.c.user_id == user_id,
            )
        )
        if row is None:
            return None
        return MemberProfile(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            callsign=str(row["callsign"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_callsign(self, guild_id: int, user_id: int) -> str | None:
        profile = await self.get(guild_id, user_id)
        return profile.callsign if profile is not None else None

    async def upsert_callsign(self, guild_id: int, user_id: int, callsign: str) -> MemberProfile:
        value = callsign.strip()
        if not value or len(value) > 40:
            raise ValueError("Callsign must contain between 1 and 40 characters")
        now = datetime.now(UTC)
        await self.client.execute(
            pg_insert(guild_member_profiles)
            .values(
                id=uuid.uuid4(),
                guild_id=guild_id,
                user_id=user_id,
                callsign=value,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_guild_member_profile",
                set_={"callsign": value, "updated_at": now},
            )
        )
        profile = await self.get(guild_id, user_id)
        if profile is None:
            raise RuntimeError("Member profile disappeared after upsert")
        return profile
