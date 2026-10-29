"""Atomic theft records and indexed serial lookups, independent of Discord and SARP."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from arrest_report_agent.infrastructure.assets import StoredAsset
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    stolen_weapon_assets,
    stolen_weapon_reports,
    weapon_theft_alerts,
)

from .models import MAX_EVIDENCE_IMAGES, StolenWeaponReport, TheftDetails, normalize_serial


class StolenWeaponRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get(self, guild_id: int, report_id: UUID) -> StolenWeaponReport | None:
        row = await self.client.fetch_one(
            sa.select(stolen_weapon_reports).where(
                stolen_weapon_reports.c.id == report_id,
                stolen_weapon_reports.c.guild_id == guild_id,
            )
        )
        if row is None:
            return None
        assets = await self.client.fetch_all(
            sa.select(stolen_weapon_assets)
            .where(
                stolen_weapon_assets.c.report_id == report_id,
            )
            .order_by(stolen_weapon_assets.c.position)
        )
        return self._report(row, tuple(self._asset(asset) for asset in assets))

    async def get_by_reference(self, guild_id: int, reference: str) -> StolenWeaponReport:
        match = re.fullmatch(r"ARM-([0-9A-Fa-f]{12})", reference.strip(), re.IGNORECASE)
        if match is None:
            raise ValueError("Usa la referencia del registro, por ejemplo ARM-A1B2C3D4E5F6.")
        prefix = match.group(1).lower()
        rows = await self.client.fetch_all(
            sa.select(stolen_weapon_reports.c.id)
            .where(
                stolen_weapon_reports.c.guild_id == guild_id,
                stolen_weapon_reports.c.id.between(UUID(prefix + "0" * 20), UUID(prefix + "f" * 20)),
            )
            .limit(2)
        )
        if not rows:
            raise ValueError("No encontré ese reporte de robo en este servidor.")
        if len(rows) != 1:
            raise ValueError("La referencia coincide con más de un registro. Solicita revisión del registro.")
        report = await self.get(guild_id, rows[0]["id"])
        if report is None:
            raise ValueError("El reporte de robo ya no está disponible.")
        return report

    async def close(
        self, guild_id: int, report_id: UUID, *, user_id: int, interaction_id: int, reason: str
    ) -> tuple[StolenWeaponReport, bool]:
        reason = reason.strip()
        if not 5 <= len(reason) <= 500:
            raise ValueError("El motivo del cierre debe tener entre 5 y 500 caracteres.")
        table = stolen_weapon_reports
        result = await self.client.execute(
            sa.update(table)
            .where(
                table.c.id == report_id,
                table.c.guild_id == guild_id,
                table.c.status == "open",
            )
            .values(
                status="closed",
                closed_by=user_id,
                closed_at=datetime.now(UTC),
                closure_reason=reason,
                closure_interaction_id=interaction_id,
            )
            .returning(table.c.id)
        )
        changed = result.scalar_one_or_none() is not None
        report = await self.get(guild_id, report_id)
        if report is None:
            raise ValueError("El reporte de robo ya no está disponible.")
        return report, changed

    async def claim_alert(
        self,
        guild_id: int,
        report_id: UUID,
        *,
        review_root_message_id: int,
        channel_id: int,
        evidence_ids: tuple[int, ...],
    ) -> bool:
        async with self.client.transaction() as connection:
            row = (
                await connection.execute(
                    sa.select(stolen_weapon_reports.c.status)
                    .where(
                        stolen_weapon_reports.c.guild_id == guild_id,
                        stolen_weapon_reports.c.id == report_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or row.status != "open":
                return False
            table = weapon_theft_alerts
            result = await connection.execute(
                insert(table)
                .values(
                    report_id=report_id,
                    review_root_message_id=review_root_message_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    evidence_ids=list(evidence_ids),
                )
                .on_conflict_do_update(
                    index_elements=[table.c.report_id, table.c.review_root_message_id],
                    set_={"status": "sending", "channel_id": channel_id, "evidence_ids": list(evidence_ids)},
                    where=table.c.status == "failed",
                )
                .returning(table.c.report_id)
            )
            return result.scalar_one_or_none() is not None

    async def mark_alert(
        self,
        guild_id: int,
        report_id: UUID,
        review_root_message_id: int,
        *,
        status: Literal["sent", "failed", "unknown"],
        message_id: int | None = None,
    ) -> None:
        table = weapon_theft_alerts
        await self.client.execute(
            sa.update(table)
            .where(
                table.c.guild_id == guild_id,
                table.c.report_id == report_id,
                table.c.review_root_message_id == review_root_message_id,
                table.c.status == "sending",
            )
            .values(status=status, message_id=message_id)
        )

    async def create(
        self,
        *,
        submission_id: int,
        guild_id: int,
        source_channel_id: int,
        destination_channel_id: int,
        registered_by: int,
        details: TheftDetails,
        assets: tuple[StoredAsset, ...],
    ) -> StolenWeaponReport:
        if not 1 <= len(assets) <= MAX_EVIDENCE_IMAGES:
            raise ValueError("El registro requiere entre 1 y 5 imágenes.")
        table = stolen_weapon_reports
        async with self.client.transaction() as connection:
            report_id = (
                await connection.execute(
                    insert(table)
                    .values(
                        id=uuid4(),
                        submission_id=submission_id,
                        guild_id=guild_id,
                        source_channel_id=source_channel_id,
                        destination_channel_id=destination_channel_id,
                        registered_by=registered_by,
                        serial=details.serial,
                        normalized_serial=details.normalized_serial,
                        description=details.description,
                        stolen_on=details.stolen_on,
                        victim_name=details.victim_name,
                        reporting_officer_name=details.reporting_officer_name,
                    )
                    .on_conflict_do_nothing(index_elements=[table.c.submission_id])
                    .returning(table.c.id)
                )
            ).scalar_one_or_none()
            if report_id is not None:
                await connection.execute(
                    stolen_weapon_assets.insert(),
                    [
                        {
                            "report_id": report_id,
                            "position": index,
                            "object_key": asset.object_key,
                            "sha256": asset.sha256,
                            "filename": asset.filename,
                            "content_type": asset.content_type,
                            "size_bytes": asset.size_bytes,
                        }
                        for index, asset in enumerate(assets)
                    ],
                )
            row = (
                (
                    await connection.execute(
                        sa.select(table).where(
                            table.c.submission_id == submission_id,
                            table.c.guild_id == guild_id,
                            table.c.registered_by == registered_by,
                        )
                    )
                )
                .mappings()
                .one()
            )
            stored = (
                (
                    await connection.execute(
                        sa.select(stolen_weapon_assets)
                        .where(
                            stolen_weapon_assets.c.report_id == row["id"],
                        )
                        .order_by(stolen_weapon_assets.c.position)
                    )
                )
                .mappings()
                .all()
            )
            return self._report(row, tuple(self._asset(item) for item in stored))

    async def claim_publication(self, guild_id: int, report_id: UUID) -> bool:
        table = stolen_weapon_reports
        result = await self.client.execute(
            sa.update(table)
            .where(
                table.c.id == report_id,
                table.c.guild_id == guild_id,
                table.c.publication_status.in_(("pending", "failed")),
            )
            .values(publication_status="publishing")
            .returning(table.c.id)
        )
        return result.scalar_one_or_none() is not None

    async def mark_published(self, guild_id: int, report_id: UUID, message_id: int) -> None:
        table = stolen_weapon_reports
        result = await self.client.execute(
            sa.update(table)
            .where(
                table.c.id == report_id,
                table.c.guild_id == guild_id,
                table.c.publication_status == "publishing",
            )
            .values(publication_status="published", message_id=message_id)
        )
        if result.rowcount != 1:
            raise RuntimeError("Missing stolen weapon publication reservation")

    async def mark_failed(self, guild_id: int, report_id: UUID) -> None:
        table = stolen_weapon_reports
        await self.client.execute(
            sa.update(table)
            .where(
                table.c.id == report_id,
                table.c.guild_id == guild_id,
                table.c.publication_status == "publishing",
            )
            .values(publication_status="failed")
        )

    async def find_by_serials(
        self, guild_id: int, serials: tuple[str, ...]
    ) -> tuple[StolenWeaponReport, ...]:
        """Return the full history for exact serial matches within one guild."""
        keys = tuple(dict.fromkeys(normalize_serial(serial) for serial in serials))
        if not keys:
            return ()
        table = stolen_weapon_reports
        async with self.client.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(table)
                        .where(
                            table.c.guild_id == guild_id,
                            table.c.normalized_serial.in_(keys),
                        )
                        .order_by(table.c.created_at.desc(), table.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return ()
            assets = (
                (
                    await connection.execute(
                        sa.select(stolen_weapon_assets)
                        .where(
                            stolen_weapon_assets.c.report_id.in_([row["id"] for row in rows]),
                        )
                        .order_by(stolen_weapon_assets.c.position)
                    )
                )
                .mappings()
                .all()
            )
            by_report: dict[UUID, list[StoredAsset]] = {}
            for asset in assets:
                by_report.setdefault(asset["report_id"], []).append(self._asset(asset))
            return tuple(self._report(row, tuple(by_report.get(row["id"], ()))) for row in rows)

    @staticmethod
    def _asset(row: sa.RowMapping) -> StoredAsset:
        return StoredAsset(
            object_key=row["object_key"],
            sha256=row["sha256"],
            filename=row["filename"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
        )

    @staticmethod
    def _report(row: sa.RowMapping, assets: tuple[StoredAsset, ...]) -> StolenWeaponReport:
        return StolenWeaponReport(
            id=row["id"],
            submission_id=row["submission_id"],
            guild_id=row["guild_id"],
            source_channel_id=row["source_channel_id"],
            destination_channel_id=row["destination_channel_id"],
            registered_by=row["registered_by"],
            created_at=row["created_at"],
            publication_status=row["publication_status"],
            message_id=row["message_id"],
            assets=assets,
            status=row["status"],
            closed_by=row["closed_by"],
            closed_at=row["closed_at"],
            closure_reason=row["closure_reason"],
            closure_interaction_id=row["closure_interaction_id"],
            details=TheftDetails(
                serial=row["serial"],
                normalized_serial=row["normalized_serial"],
                description=row["description"],
                stolen_on=row["stolen_on"],
                victim_name=row["victim_name"],
                reporting_officer_name=row["reporting_officer_name"],
            ),
        )
