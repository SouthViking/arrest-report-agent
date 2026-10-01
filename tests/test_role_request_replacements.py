from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds, role_requests
from arrest_report_agent.role_requests.models import (
    RoleRequestDecision,
    RoleRequestKind,
    RoleRequestStatus,
    StoredRoleRequestStatus,
)
from arrest_report_agent.role_requests.repository import RoleRequestBusyError, RoleRequestRepository


@pytest.fixture
async def replacement_repo(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    for guild in (901, 902):
        await client.execute(discord_guilds.insert().values(discord_id=guild, name="Replacement QA"))
    try:
        yield RoleRequestRepository(client)
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id.in_((901, 902))))
        await client.close()


async def reserve(repo, interaction, *, guild=901, user=100, kind=RoleRequestKind.NORMAL):
    return await repo.reserve(
        guild_id=guild, interaction_id=interaction, requester_id=user, kind=kind,
        add_role_ids=(201,), remove_role_ids=(), reason="Solicitud QA", public_channel_id=301,
    )


async def publish(repo, request):
    return await repo.mark_published(
        request.id, public_message_id=request.interaction_id + 1000,
        review_messages=((401, request.interaction_id + 2000), (402, request.interaction_id + 3000)),
    )


@pytest.mark.parametrize("old_kind", list(RoleRequestKind))
@pytest.mark.parametrize("new_kind", list(RoleRequestKind))
async def test_replacement_preserves_history_and_cancels_any_kind(replacement_repo, old_kind, new_kind):
    repo = replacement_repo
    old, _ = await reserve(repo, 1, kind=old_kind)
    await publish(repo, old)
    other_user, _ = await reserve(repo, 2, user=101)
    await publish(repo, other_user)
    other_guild, _ = await reserve(repo, 3, guild=902)
    await publish(repo, other_guild)
    new, created = await reserve(repo, 4, kind=new_kind)
    assert created
    assert (await repo.get(old.id)).status is StoredRoleRequestStatus.PENDING
    assert await repo.claim_decision(old.id) is None  # Publication must finish first.
    await publish(repo, new)
    cancelled = await repo.get(old.id)
    assert cancelled.status is StoredRoleRequestStatus.CANCELLED
    assert cancelled.replaced_by_request_id == new.id
    assert cancelled.decided_at and cancelled.cancellation_sync_due
    assert cancelled.reviewer_id is None  # No invented supervisor or applied roles.
    assert cancelled.approved_add_role_ids == ()
    assert cancelled.unapproved_add_role_ids == (201,)
    assert len(await repo.review_messages(old.id)) == 2
    assert await repo.claim_decision(old.id) is None
    assert (await repo.get(new.id)).status is StoredRoleRequestStatus.PENDING
    assert (await repo.get(other_user.id)).status is StoredRoleRequestStatus.PENDING
    assert (await repo.get(other_guild.id)).status is StoredRoleRequestStatus.PENDING
    assert [r.id for r in await repo.list_cancellation_sync(replacement_id=new.id)] == [old.id]
    await repo.set_cancellation_sync_due(old.id, None)
    assert await repo.list_cancellation_sync() == ()
    repeated, created_again = await reserve(repo, 4, kind=new_kind)
    assert repeated.id == new.id and not created_again


async def test_failed_publication_retains_previous_and_releases_approval(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 10)
    await publish(repo, old)
    draft, _ = await reserve(repo, 11)
    await repo.mark_failed(draft.id)
    assert (await repo.get(old.id)).status is StoredRoleRequestStatus.PENDING
    assert await repo.list_cancellation_sync() == ()
    assert await repo.claim_decision(old.id) is not None


async def test_database_publication_failure_rolls_back_cancellations(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 20)
    await publish(repo, old)
    draft, _ = await reserve(repo, 21)
    with pytest.raises(Exception, match="PostgreSQL transaction failed"):
        # A duplicate mirror is rejected after cancellation inside the same transaction.
        await repo.mark_published(draft.id, public_message_id=999,
                                 review_messages=((401, 2020),))
    assert (await repo.get(old.id)).status is StoredRoleRequestStatus.PENDING
    assert (await repo.get(draft.id)).status is StoredRoleRequestStatus.PUBLISHING
    await repo.mark_failed(draft.id)
    assert await repo.claim_decision(old.id) is not None


async def test_processing_is_not_interrupted_then_new_request_can_follow(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 30)
    await publish(repo, old)
    assert await repo.claim_decision(old.id)
    with pytest.raises(RoleRequestBusyError):
        await reserve(repo, 31)
    assert (await repo.get(old.id)).status is StoredRoleRequestStatus.PROCESSING
    await repo.finalize(old.id, RoleRequestDecision(
        status=RoleRequestStatus.APPROVED, reviewer_id=501,
        approved_add_role_ids=(201,), decided_at=datetime.now(UTC),
    ))
    new, _ = await reserve(repo, 31)
    await publish(repo, new)
    assert (await repo.get(old.id)).status is StoredRoleRequestStatus.APPROVED


async def test_two_submissions_cannot_publish_in_parallel(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 40)
    await publish(repo, old)
    outcomes = await asyncio.gather(reserve(repo, 41), reserve(repo, 42), return_exceptions=True)
    assert sum(isinstance(outcome, RoleRequestBusyError) for outcome in outcomes) == 1
    new, _ = next(outcome for outcome in outcomes if isinstance(outcome, tuple))
    await publish(repo, new)
    assert (await repo.get(old.id)).replaced_by_request_id == new.id
    rows = await repo.client.fetch_all(sa.select(role_requests).where(
        role_requests.c.guild_id == 901,
        role_requests.c.status.in_(("pending", "processing")),
    ))
    assert len(rows) == 1 and rows[0]["id"] == new.id


async def test_approval_racing_submission_has_one_winner(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 50)
    await publish(repo, old)
    claim, outcome = await asyncio.gather(repo.claim_decision(old.id), reserve(repo, 51),
                                          return_exceptions=True)
    if claim is not None:
        assert isinstance(outcome, RoleRequestBusyError)
        assert (await repo.get(old.id)).status is StoredRoleRequestStatus.PROCESSING
    else:
        assert isinstance(outcome, tuple)
        await publish(repo, outcome[0])
        assert (await repo.get(old.id)).status is StoredRoleRequestStatus.CANCELLED


async def test_all_legacy_pending_rows_are_cancelled(replacement_repo):
    repo = replacement_repo
    old, _ = await reserve(repo, 60)
    await publish(repo, old)
    # Simulate data predating the uniqueness constraint, restore it in all cases.
    await repo.client.execute(sa.text("DROP INDEX uq_role_request_live_requester"))
    try:
        second_id = uuid4()
        await repo.client.execute(role_requests.insert().values(
            id=second_id, guild_id=901, requester_id=100, interaction_id=61,
            kind="retirement", status="pending", public_channel_id=301,
            add_role_ids=[202], remove_role_ids=[],
        ))
        new, _ = await reserve(repo, 62)
        await publish(repo, new)
        assert {r.id for r in await repo.list_cancellation_sync()} == {old.id, second_id}
    finally:
        await repo.client.execute(role_requests.delete().where(role_requests.c.guild_id == 901))
        await repo.client.execute(sa.text("CREATE UNIQUE INDEX uq_role_request_live_requester "
                                         "ON role_requests (guild_id,requester_id) "
                                         "WHERE status IN ('pending','processing')"))
