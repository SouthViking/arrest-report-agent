from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.role_requests.models import (
    RoleRequestDecision,
    RoleRequestKind,
    RoleRequestShiftChange,
    RoleRequestStatus,
    StoredRoleRequestStatus,
)
from arrest_report_agent.role_requests.repository import (
    PendingRoleRequestError,
    RoleRequestRepository,
)


@pytest.mark.asyncio
async def test_role_request_repository_is_idempotent_concurrent_and_mirrored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 975432101234567890
    requester_id = 975432101234567891
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(
            discord_guilds.insert().values(discord_id=guild_id, name="Role request repository QA")
        )
        repository = RoleRequestRepository(client)

        async def reserve(interaction_id: int):
            return await repository.reserve(
                guild_id=guild_id,
                interaction_id=interaction_id,
                requester_id=requester_id,
                kind=RoleRequestKind.NORMAL,
                add_role_ids=(101,),
                remove_role_ids=(102,),
                reason="Cambio operativo",
                public_channel_id=201,
            )

        outcomes = await asyncio.gather(
            reserve(301),
            reserve(302),
            return_exceptions=True,
        )
        created = [outcome for outcome in outcomes if isinstance(outcome, tuple)]
        blocked = [outcome for outcome in outcomes if isinstance(outcome, PendingRoleRequestError)]
        assert len(created) == 1
        assert len(blocked) == 1
        request, was_created = created[0]
        assert was_created is True

        duplicate, duplicate_created = await reserve(request.interaction_id)
        assert duplicate.id == request.id
        assert duplicate_created is False

        published = await repository.mark_published(
            request.id,
            public_message_id=401,
            review_messages=((501, 601), (502, 602)),
        )
        assert published.status is StoredRoleRequestStatus.PENDING
        assert len(await repository.review_messages(request.id)) == 2
        assert (await repository.get_by_review_message(guild_id, 502, 602)).id == request.id  # type: ignore[union-attr]
        assert (await repository.get_by_public_message(guild_id, 201, 401)).id == request.id  # type: ignore[union-attr]

        claims = await asyncio.gather(
            repository.claim_decision(request.id),
            repository.claim_decision(request.id),
        )
        assert sum(claim is not None for claim in claims) == 1
        decision = RoleRequestDecision(
            status=RoleRequestStatus.APPROVED,
            reviewer_id=701,
            approved_add_role_ids=(101,),
            approved_remove_role_ids=(102,),
            decided_at=datetime.now(UTC),
        )
        finalized = await repository.finalize(request.id, decision)
        assert finalized.status is StoredRoleRequestStatus.APPROVED

        after_restart = RoleRequestRepository(client)
        restored = await after_restart.get(request.id)
        assert restored is not None
        assert restored.reviewer_id == 701
        assert restored.decision_comment is None

        ftt_entry, ftt_created = await repository.reserve(
            guild_id=guild_id,
            interaction_id=303,
            requester_id=requester_id,
            kind=RoleRequestKind.FTT_ENTRY,
            add_role_ids=(101, 103, 104, 105, 106, 108),
            remove_role_ids=(107,),
            shift_change=RoleRequestShiftChange(target_role_id=108, previous_role_ids=(107,)),
            reason="Ganton · <@&106>",
            public_channel_id=201,
        )
        assert ftt_created is True
        assert ftt_entry.kind is RoleRequestKind.FTT_ENTRY
        restored_entry = await RoleRequestRepository(client).get(ftt_entry.id)
        assert restored_entry is not None
        assert restored_entry.shift_change == ftt_entry.shift_change
        adapted = restored_entry.as_legacy_request(source_channel_id=201, source_message_id=402)
        assert adapted.shift_change == ftt_entry.shift_change
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
