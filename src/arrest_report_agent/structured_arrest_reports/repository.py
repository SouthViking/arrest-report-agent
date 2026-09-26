"""Transactional persistence for structured arrest-report submissions."""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    arrest_report_assets,
    arrest_report_submissions,
    report_arrest_bindings,
)

from .models import (
    ArrestReportAsset,
    ArrestReportSubmission,
    InitialReviewStatus,
    PendingArrestReportAsset,
    PublicationStatus,
)


class StructuredArrestReportRepository:
    """Reserve, publish and resolve structured report roots without storing SARP content."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        guild_id: int,
        interaction_id: int,
        owner_user_id: int,
        parent_channel_id: int,
        sarp_arrest_id: int | None,
        suspect_name: str,
    ) -> tuple[ArrestReportSubmission, bool]:
        normalized_name = " ".join(suspect_name.split())
        async with self.client.transaction() as connection:
            reservation_key = f"structured-arrest:interaction:{interaction_id}"
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": reservation_key},
            )
            existing = (
                (
                    await connection.execute(
                        sa.select(arrest_report_submissions).where(
                            arrest_report_submissions.c.interaction_id == interaction_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["publication_status"] == PublicationStatus.FAILED.value:
                    await connection.execute(
                        arrest_report_assets.delete().where(
                            arrest_report_assets.c.submission_id == existing["id"]
                        )
                    )
                    retried = (
                        (
                            await connection.execute(
                                arrest_report_submissions.update()
                                .where(arrest_report_submissions.c.id == existing["id"])
                                .values(
                                    interaction_id=interaction_id,
                                    owner_user_id=owner_user_id,
                                    parent_channel_id=parent_channel_id,
                                    root_message_id=None,
                                    thread_id=None,
                                    sarp_arrest_id=sarp_arrest_id,
                                    suspect_name=normalized_name,
                                    publication_status=PublicationStatus.PUBLISHING.value,
                                    initial_review_status=InitialReviewStatus.PENDING.value,
                                    updated_at=sa.func.now(),
                                )
                                .returning(*arrest_report_submissions.c)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    return _submission(retried), True
                return await self._with_assets(connection, existing), False
            row = (
                (
                    await connection.execute(
                        arrest_report_submissions.insert()
                        .values(
                            id=uuid.uuid4(),
                            guild_id=guild_id,
                            interaction_id=interaction_id,
                            owner_user_id=owner_user_id,
                            parent_channel_id=parent_channel_id,
                            sarp_arrest_id=sarp_arrest_id,
                            suspect_name=normalized_name,
                            publication_status=PublicationStatus.PUBLISHING.value,
                            initial_review_status=InitialReviewStatus.PENDING.value,
                        )
                        .returning(*arrest_report_submissions.c)
                    )
                )
                .mappings()
                .one()
            )
        return _submission(row), True

    async def get_by_interaction(self, interaction_id: int) -> ArrestReportSubmission | None:
        return await self._get(arrest_report_submissions.c.interaction_id == interaction_id)

    async def get_by_id(self, submission_id: uuid.UUID) -> ArrestReportSubmission | None:
        return await self._get(arrest_report_submissions.c.id == submission_id)

    async def get_by_report(self, guild_id: int, sarp_arrest_id: int) -> ArrestReportSubmission | None:
        async with self.client.connection() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(arrest_report_submissions)
                        .where(
                            arrest_report_submissions.c.guild_id == guild_id,
                            arrest_report_submissions.c.sarp_arrest_id == sarp_arrest_id,
                            arrest_report_submissions.c.publication_status
                            == PublicationStatus.PUBLISHED.value,
                        )
                        .order_by(
                            arrest_report_submissions.c.created_at.desc(),
                            arrest_report_submissions.c.id.desc(),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return await self._with_assets(connection, row) if row is not None else None

    async def get_published_by_roots(
        self,
        guild_id: int,
        root_message_ids: tuple[int, ...],
    ) -> tuple[ArrestReportSubmission, ...]:
        """Resolve published structured roots in one bounded database query."""

        unique_root_ids = tuple(dict.fromkeys(root_message_ids))
        if not unique_root_ids:
            return ()
        async with self.client.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(arrest_report_submissions).where(
                            arrest_report_submissions.c.guild_id == guild_id,
                            arrest_report_submissions.c.root_message_id.in_(unique_root_ids),
                            arrest_report_submissions.c.publication_status
                            == PublicationStatus.PUBLISHED.value,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_submission(row) for row in rows)

    async def get_by_root(self, guild_id: int, root_message_id: int) -> ArrestReportSubmission | None:
        return await self._get(
            arrest_report_submissions.c.guild_id == guild_id,
            arrest_report_submissions.c.root_message_id == root_message_id,
        )

    async def get_by_thread(self, guild_id: int, thread_id: int) -> ArrestReportSubmission | None:
        return await self._get(
            arrest_report_submissions.c.guild_id == guild_id,
            arrest_report_submissions.c.thread_id == thread_id,
        )

    async def recover_published_mapping(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        thread_id: int,
    ) -> ArrestReportSubmission | None:
        """Repair one publication whose Discord mapping was rolled back after being returned.

        The arrest binding was committed separately after the Discord message and thread
        were created. It is therefore a safe recovery source when all ownership, channel,
        report and timing fields identify exactly one unfinished submission.
        """

        async with self.client.transaction() as connection:
            binding = (
                (
                    await connection.execute(
                        sa.select(report_arrest_bindings).where(
                            report_arrest_bindings.c.guild_id == guild_id,
                            report_arrest_bindings.c.root_message_id == root_message_id,
                            report_arrest_bindings.c.thread_id == thread_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is None:
                return None
            candidates = (
                (
                    await connection.execute(
                        sa.select(arrest_report_submissions)
                        .where(
                            arrest_report_submissions.c.guild_id == guild_id,
                            arrest_report_submissions.c.owner_user_id
                            == binding["bound_by_user_id"],
                            arrest_report_submissions.c.parent_channel_id
                            == binding["parent_channel_id"],
                            arrest_report_submissions.c.sarp_arrest_id
                            == binding["sarp_arrest_id"],
                            arrest_report_submissions.c.publication_status
                            == PublicationStatus.PUBLISHING.value,
                            arrest_report_submissions.c.root_message_id.is_(None),
                            arrest_report_submissions.c.thread_id.is_(None),
                            arrest_report_submissions.c.created_at <= binding["created_at"],
                            arrest_report_submissions.c.created_at
                            >= binding["created_at"] - timedelta(minutes=5),
                        )
                        .order_by(arrest_report_submissions.c.created_at.desc())
                        .limit(2)
                    )
                )
                .mappings()
                .all()
            )
            if len(candidates) != 1:
                return None
            row = (
                (
                    await connection.execute(
                        arrest_report_submissions.update()
                        .where(
                            arrest_report_submissions.c.id == candidates[0]["id"],
                            arrest_report_submissions.c.publication_status
                            == PublicationStatus.PUBLISHING.value,
                            arrest_report_submissions.c.root_message_id.is_(None),
                            arrest_report_submissions.c.thread_id.is_(None),
                        )
                        .values(
                            publication_status=PublicationStatus.PUBLISHED.value,
                            root_message_id=root_message_id,
                            thread_id=thread_id,
                            updated_at=sa.func.now(),
                        )
                        .returning(*arrest_report_submissions.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _submission(row) if row is not None else None

    async def replace_assets(
        self,
        submission_id: uuid.UUID,
        assets: tuple[PendingArrestReportAsset, ...],
    ) -> tuple[ArrestReportAsset, ...]:
        async with self.client.transaction() as connection:
            await connection.execute(
                arrest_report_assets.delete().where(arrest_report_assets.c.submission_id == submission_id)
            )
            rows: list[sa.RowMapping] = []
            for asset in assets:
                row = (
                    (
                        await connection.execute(
                            arrest_report_assets.insert()
                            .values(
                                id=uuid.uuid4(),
                                submission_id=submission_id,
                                **asset.model_dump(mode="json"),
                            )
                            .returning(*arrest_report_assets.c)
                        )
                    )
                    .mappings()
                    .one()
                )
                rows.append(row)
        return tuple(_asset(row) for row in rows)

    async def sync_assets(
        self,
        submission_id: uuid.UUID,
        *,
        expected_ids: frozenset[uuid.UUID],
        assets: tuple[ArrestReportAsset | PendingArrestReportAsset, ...],
    ) -> ArrestReportSubmission:
        """Apply one image edit while retaining IDs of all unchanged assets."""

        async with self.client.transaction() as connection:
            await connection.execute(
                sa.select(arrest_report_submissions.c.id)
                .where(arrest_report_submissions.c.id == submission_id)
                .with_for_update()
            )
            current = (
                (
                    await connection.execute(
                        sa.select(arrest_report_assets.c.id)
                        .where(arrest_report_assets.c.submission_id == submission_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if frozenset(current) != expected_ids:
                raise ValueError("Los adjuntos cambiaron. Abre el gestor nuevamente.")
            retained_ids = {asset.id for asset in assets if isinstance(asset, ArrestReportAsset)}
            if not retained_ids.issubset(expected_ids) or len(retained_ids) != sum(
                isinstance(asset, ArrestReportAsset) for asset in assets
            ):
                raise ValueError("La selección de adjuntos ya no es válida.")
            await connection.execute(
                arrest_report_assets.delete().where(
                    arrest_report_assets.c.submission_id == submission_id,
                    arrest_report_assets.c.id.not_in(retained_ids),
                )
            )
            # Positions are unique per category; move retained rows aside first.
            for index, asset in enumerate(assets):
                if isinstance(asset, ArrestReportAsset):
                    await connection.execute(
                        arrest_report_assets.update()
                        .where(arrest_report_assets.c.id == asset.id)
                        .values(position=100 + index)
                    )
            for index, asset in enumerate(assets):
                position = sum(prior.category == asset.category for prior in assets[:index])
                if isinstance(asset, ArrestReportAsset):
                    await connection.execute(
                        arrest_report_assets.update()
                        .where(arrest_report_assets.c.id == asset.id)
                        .values(position=position)
                    )
                else:
                    await connection.execute(
                        arrest_report_assets.insert().values(
                            id=uuid.uuid4(),
                            submission_id=submission_id,
                            **asset.model_copy(update={"position": position}).model_dump(mode="json"),
                        )
                    )
            await connection.execute(
                arrest_report_submissions.update()
                .where(arrest_report_submissions.c.id == submission_id)
                .values(updated_at=sa.func.now())
            )
            row = (
                (
                    await connection.execute(
                        sa.select(arrest_report_submissions).where(
                            arrest_report_submissions.c.id == submission_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            return await self._with_assets(connection, row)

    async def mark_published(
        self,
        submission_id: uuid.UUID,
        *,
        root_message_id: int,
        thread_id: int,
    ) -> ArrestReportSubmission:
        return await self._transition(
            submission_id,
            publication_status=PublicationStatus.PUBLISHED,
            values={"root_message_id": root_message_id, "thread_id": thread_id},
        )

    async def mark_publication_failed(self, submission_id: uuid.UUID) -> None:
        await self._transition(
            submission_id,
            publication_status=PublicationStatus.FAILED,
        )

    async def claim_initial_review(self, submission_id: uuid.UUID) -> bool:
        result = await self.client.execute(
            arrest_report_submissions.update()
            .where(
                arrest_report_submissions.c.id == submission_id,
                arrest_report_submissions.c.initial_review_status == InitialReviewStatus.PENDING.value,
            )
            .values(
                initial_review_status=InitialReviewStatus.RUNNING.value,
                updated_at=sa.func.now(),
            )
        )
        return bool(result.rowcount)

    async def finish_initial_review(self, submission_id: uuid.UUID, *, success: bool) -> None:
        await self.client.execute(
            arrest_report_submissions.update()
            .where(arrest_report_submissions.c.id == submission_id)
            .values(
                initial_review_status=(
                    InitialReviewStatus.COMPLETED.value if success else InitialReviewStatus.FAILED.value
                ),
                updated_at=sa.func.now(),
            )
        )

    async def _get(self, *filters: ColumnElement[bool]) -> ArrestReportSubmission | None:
        async with self.client.connection() as connection:
            row = (
                (await connection.execute(sa.select(arrest_report_submissions).where(*filters)))
                .mappings()
                .one_or_none()
            )
            return await self._with_assets(connection, row) if row is not None else None

    async def _transition(
        self,
        submission_id: uuid.UUID,
        *,
        publication_status: PublicationStatus,
        values: dict[str, object] | None = None,
    ) -> ArrestReportSubmission:
        async with self.client.transaction() as connection:
            row = await self.client.fetch_one(
                arrest_report_submissions.update()
                .where(arrest_report_submissions.c.id == submission_id)
                .values(
                    publication_status=publication_status.value,
                    updated_at=sa.func.now(),
                    **(values or {}),
                )
                .returning(*arrest_report_submissions.c),
                connection=connection,
            )
        if row is None:
            raise LookupError("Structured arrest report submission not found")
        return _submission(row)

    @staticmethod
    async def _with_assets(
        connection: AsyncConnection,
        row: sa.RowMapping,
    ) -> ArrestReportSubmission:
        assets = (
            (
                await connection.execute(
                    sa.select(arrest_report_assets)
                    .where(arrest_report_assets.c.submission_id == row["id"])
                    .order_by(
                        arrest_report_assets.c.category,
                        arrest_report_assets.c.position,
                    )
                )
            )
            .mappings()
            .all()
        )
        return _submission(row, assets=tuple(_asset(asset) for asset in assets))


def _submission(
    row: sa.RowMapping,
    *,
    assets: tuple[ArrestReportAsset, ...] = (),
) -> ArrestReportSubmission:
    return ArrestReportSubmission.model_validate({**dict(row), "assets": assets})


def _asset(row: sa.RowMapping) -> ArrestReportAsset:
    return ArrestReportAsset.model_validate(dict(row))
