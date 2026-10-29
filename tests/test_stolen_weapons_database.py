"""Real PostgreSQL migration, idempotency, guild isolation and durable serial history."""

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from test_stolen_weapons import DESTINATION, GUILD, SOURCE, USER, _details, _file

from arrest_report_agent.infrastructure.postgres import (
    DatabaseUnavailableError,
    PostgresClient,
    PostgresClientConfig,
)
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_access_policies,
    guild_access_policy_members,
    guild_config_revisions,
    guild_feature_settings,
    stolen_weapon_assets,
    stolen_weapon_reports,
    weapon_theft_alerts,
)
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage
from arrest_report_agent.stolen_weapons.repository import StolenWeaponRepository
from arrest_report_agent.stolen_weapons.service import StolenWeaponService


async def test_migration_and_durable_serial_history(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261028_0096")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    revisions = {}
    custom_payload = {"source_channel_ids": [71], "destination_channel_id": 72}
    try:
        async with client.transaction() as connection:
            for guild in (GUILD, 999):
                await connection.execute(discord_guilds.insert().values(discord_id=guild))
                for number, status in enumerate(("draft", "published", "archived", "custom", "no_roles"), 1):
                    revision = uuid4()
                    revisions[(guild, status)] = revision
                    await connection.execute(
                        guild_config_revisions.insert().values(
                            id=revision,
                            guild_id=guild,
                            revision=number,
                            status="draft" if status in {"custom", "no_roles"} else status,
                        )
                    )
                    if status != "no_roles":
                        policy_id = uuid4()
                        await connection.execute(
                            guild_access_policies.insert().values(
                                id=policy_id,
                                config_revision_id=revision,
                                policy_key="operational_staff_roles",
                                subject_type="role",
                                mode="restricted",
                            )
                        )
                        await connection.execute(
                            guild_access_policy_members.insert().values(
                                policy_id=policy_id,
                                snowflake_id=50,
                            )
                        )
                    if status == "custom":
                        await connection.execute(
                            guild_feature_settings.insert().values(
                                config_revision_id=revision,
                                feature_key="stolen_weapons",
                                enabled=False,
                                schema_version=1,
                                payload=custom_payload,
                            )
                        )
        await asyncio.to_thread(command.upgrade, alembic, "head")
        rows = await client.fetch_all(
            sa.select(guild_feature_settings).where(
                guild_feature_settings.c.feature_key == "stolen_weapons",
            )
        )
        by_revision = {row["config_revision_id"]: row for row in rows}
        for (guild, status), revision in revisions.items():
            if status == "custom":
                assert by_revision[revision]["payload"] == custom_payload
                assert not by_revision[revision]["enabled"]
            elif guild == GUILD and status in {"draft", "published", "no_roles"}:
                assert by_revision[revision]["enabled"] is (status != "no_roles")
                assert by_revision[revision]["payload"] == {
                    "source_channel_ids": [SOURCE],
                    "destination_channel_id": DESTINATION,
                }
            else:
                assert revision not in by_revision

        storage = MemoryObjectStorage()
        repository = StolenWeaponRepository(client)
        service = StolenWeaponService(repository, storage)
        values = dict(
            submission_id=1001,
            guild_id=GUILD,
            source_channel_id=SOURCE,
            destination_channel_id=DESTINATION,
            registered_by=USER,
            details=_details(),
            files=(_file(),),
            max_file_bytes=10000,
            max_total_bytes=50000,
        )
        first, retried = await asyncio.gather(service.register(**values), service.register(**values))
        assert first.id == retried.id
        assert len(await client.fetch_all(sa.select(stolen_weapon_reports))) == 1
        assert len(await client.fetch_all(sa.select(stolen_weapon_assets))) == 1
        assert first.created_at.utcoffset().total_seconds() == 0
        assert first.details.stolen_on.isoformat() == "2026-10-27"
        assert await storage.get(first.assets[0].object_key) == _file().payload
        assert first.assets[0].sha256 in first.assets[0].object_key

        assert not await repository.claim_publication(999, first.id)
        claims = await asyncio.gather(*(repository.claim_publication(GUILD, first.id) for _ in range(2)))
        assert sorted(claims) == [False, True]
        await repository.mark_failed(GUILD, first.id)
        assert await repository.claim_publication(GUILD, first.id)
        await repository.mark_published(GUILD, first.id, 555)
        assert not await repository.claim_publication(GUILD, first.id)
        await repository.mark_failed(GUILD, first.id)  # a delivered report cannot become retryable

        # Same serial, another theft: preserve both events rather than overwrite or reject.
        second = await service.register(
            **{
                **values,
                "submission_id": 1002,
                "details": _details(serial="SA-001234", stolen_on="28/10/2026"),
            }
        )
        third = await service.register(
            **{**values, "submission_id": 1003, "details": _details(serial="000012")}
        )
        await service.register(**{**values, "submission_id": 1004, "guild_id": 999})
        # Read through a new repository instance to prove persistence across sessions/restarts.
        repository = StolenWeaponRepository(client)
        history = await repository.find_by_serials(GUILD, (" sa-001234 ",))
        assert {report.id for report in history} == {first.id, second.id}
        published = next(report for report in history if report.id == first.id)
        assert published.publication_status == "published"
        assert published.jump_url == f"https://discord.com/channels/{GUILD}/{DESTINATION}/555"
        assert published.registered_by == USER and published.assets == first.assets
        assert len(await repository.find_by_serials(999, ("sa-001234",))) == 1
        assert await repository.find_by_serials(GUILD, ("SA001234", "12")) == ()
        assert await repository.find_by_serials(GUILD, ()) == ()
        batch = await repository.find_by_serials(GUILD, ("sa-001234", "000012", "SA-001234"))
        assert {report.id for report in batch} == {first.id, second.id, third.id}

        # Public evidence queries expose only open links, including unpublished records,
        # without loading private identities/evidence or creating notification deliveries.
        links = await repository.find_open_report_links(GUILD, (" sa-001234 ", "SA-001234"))
        assert {link.reference for link in links} == {first.reference, second.reference}
        first_link = next(link for link in links if link.reference == first.reference)
        assert first_link.jump_url == published.jump_url
        assert next(link for link in links if link.reference == second.reference).jump_url is None
        assert _details().victim_name not in str(links) and _details().description not in str(links)
        assert len(await repository.find_open_report_links(999, ("SA-001234",))) == 1
        assert await repository.find_open_report_links(GUILD, ("SA001234", "12")) == ()
        assert await repository.find_open_report_links(GUILD, ()) == ()
        assert len(await client.fetch_all(sa.select(weapon_theft_alerts))) == 0

        # Alerts deduplicate across independent instances, retries and concurrent runs.
        alert = dict(review_root_message_id=8001, channel_id=DESTINATION, evidence_ids=(5303358,))
        claimed = await asyncio.gather(*(repository.claim_alert(GUILD, first.id, **alert) for _ in range(2)))
        assert sorted(claimed) == [False, True]
        await repository.mark_alert(GUILD, first.id, 8001, status="unknown")
        assert not await StolenWeaponRepository(client).claim_alert(GUILD, first.id, **alert)
        assert not await repository.claim_alert(999, first.id, **alert)
        next_alert = {**alert, "review_root_message_id": 8002}
        assert await repository.claim_alert(GUILD, first.id, **next_alert)
        await repository.mark_alert(GUILD, first.id, 8002, status="failed")
        assert await repository.claim_alert(GUILD, first.id, **next_alert)
        await repository.mark_alert(GUILD, first.id, 8002, status="sent", message_id=444)
        assert not await repository.claim_alert(GUILD, first.id, **next_alert)
        assert len(await client.fetch_all(sa.select(weapon_theft_alerts))) == 2

        # Close one event, retaining the second open event for the same serial.
        assert (await repository.get_by_reference(GUILD, first.reference)).id == first.id
        with pytest.raises(ValueError, match="No encontré"):
            await repository.get_by_reference(999, first.reference)
        closure = dict(user_id=USER, interaction_id=9001, reason="Arma encontrada y seguimiento terminado")
        closed, changed = await repository.close(GUILD, first.id, **closure)
        assert changed and closed.status == "closed" and closed.closed_by == USER
        assert closed.closed_at.utcoffset().total_seconds() == 0
        assert closed.assets == first.assets and closed.message_id == 555
        old, changed = await repository.close(
            GUILD, first.id, user_id=555, interaction_id=9002, reason="Otro motivo"
        )
        assert not changed and old.closed_by == USER and old.closure_reason == closure["reason"]
        assert old.closure_interaction_id == 9001
        assert not await repository.claim_alert(GUILD, first.id, **{**alert, "review_root_message_id": 8003})
        open_links = await repository.find_open_report_links(GUILD, ("SA-001234",))
        assert [link.reference for link in open_links] == [second.reference]
        from arrest_report_agent.stolen_weapons.lookup import WeaponTheftLookupService

        lookup = WeaponTheftLookupService(repository)
        result = (await lookup.lookup(GUILD, ("sa-001234",)))[0]
        assert result.status == "reported_stolen" and len(result.reports) == 2
        races = await asyncio.gather(
            repository.close(GUILD, second.id, user_id=USER, interaction_id=9010, reason="Cierre verificado"),
            repository.close(GUILD, second.id, user_id=555, interaction_id=9011, reason="Segundo cierre"),
        )
        assert sorted(changed for _report, changed in races) == [False, True]
        assert (await lookup.lookup(GUILD, ("sa-001234",)))[0].status == "closed"
        assert len(await repository.find_by_serials(GUILD, ("sa-001234",))) == 2
        assert await repository.find_open_report_links(GUILD, ("sa-001234",)) == ()

        # A failed evidence insert rolls back its parent, avoiding incomplete theft records.
        with pytest.raises(DatabaseUnavailableError):
            await repository.create(
                submission_id=1005,
                guild_id=GUILD,
                source_channel_id=SOURCE,
                destination_channel_id=DESTINATION,
                registered_by=USER,
                details=_details(),
                assets=(replace(first.assets[0], size_bytes=-1),),
            )
        assert (
            await client.fetch_one(
                sa.select(stolen_weapon_reports).where(
                    stolen_weapon_reports.c.submission_id == 1005,
                )
            )
            is None
        )

        # Guild deletion cascades through report metadata and evidence references.
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == 999))
        assert await repository.find_by_serials(999, ("sa-001234",)) == ()
        await asyncio.to_thread(command.downgrade, alembic, "20261028_0096")
        async with client.connection() as connection:
            assert await connection.scalar(sa.text("SELECT to_regclass('stolen_weapon_reports')")) is None
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
