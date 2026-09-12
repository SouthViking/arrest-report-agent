"""PostgreSQL persistence for generic channel-scoped blacklists."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.config import BlacklistTypeOptions, PcuProfileKind
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    blacklist_counters,
    blacklist_entries,
)

from .models import (
    BlacklistEntry,
    BlacklistEntryStatus,
    normalize_subject_name,
    parse_lookup,
    parse_pcu_profile_urls,
    parse_reference,
    subject_name_key,
)


class BlacklistRepository:
    """Allocate per-type references and keep Discord publication state idempotent."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        guild_id: int,
        channel_id: int,
        interaction_id: int,
        author_user_id: int,
        type_definition: BlacklistTypeOptions,
        raw_pcu_urls: str,
        raw_subject_name: str,
        raw_reason: str,
    ) -> tuple[BlacklistEntry, bool]:
        profile_ids, pcu_urls = parse_pcu_profile_urls(
            raw_pcu_urls,
            expected_kind=type_definition.profile_kind,
            max_profiles=type_definition.max_profiles,
        )
        subject_name = normalize_subject_name(raw_subject_name)
        name_key = subject_name_key(subject_name)
        reason = raw_reason.strip()
        if not 10 <= len(reason) <= 1_000:
            raise ValueError("La razón debe contener entre 10 y 1000 caracteres.")
        timestamp = datetime.now(UTC)
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"blacklist:{interaction_id}"},
            )
            existing = (
                (
                    await connection.execute(
                        sa.select(blacklist_entries).where(
                            blacklist_entries.c.interaction_id == interaction_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return self._entry_from_row(existing), False
            number = int(
                (
                    await connection.execute(
                        pg_insert(blacklist_counters)
                        .values(
                            guild_id=guild_id,
                            type_key=type_definition.key,
                            last_number=1,
                        )
                        .on_conflict_do_update(
                            index_elements=[
                                blacklist_counters.c.guild_id,
                                blacklist_counters.c.type_key,
                            ],
                            set_={
                                "last_number": blacklist_counters.c.last_number + 1,
                                "updated_at": sa.func.now(),
                            },
                        )
                        .returning(blacklist_counters.c.last_number)
                    )
                ).scalar_one()
            )
            row = (
                (
                    await connection.execute(
                        blacklist_entries.insert()
                        .values(
                            id=uuid.uuid4(),
                            guild_id=guild_id,
                            type_key=type_definition.key,
                            type_name=type_definition.display_name.strip(),
                            reference_prefix=type_definition.reference_prefix,
                            number=number,
                            channel_id=channel_id,
                            interaction_id=interaction_id,
                            author_user_id=author_user_id,
                            profile_kind=type_definition.profile_kind.value,
                            pcu_profile_ids=list(profile_ids),
                            pcu_urls=list(pcu_urls),
                            subject_label=type_definition.subject_label.strip(),
                            subject_name=subject_name,
                            subject_name_key=name_key,
                            reason=reason,
                            status=BlacklistEntryStatus.PUBLISHING.value,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                        .returning(*blacklist_entries.c)
                    )
                )
                .mappings()
                .one()
            )
        return self._entry_from_row(row), True

    async def mark_active(self, entry_id: uuid.UUID, message_id: int) -> BlacklistEntry:
        return await self._transition(
            entry_id,
            status=BlacklistEntryStatus.ACTIVE,
            message_id=message_id,
        )

    async def mark_failed(self, entry_id: uuid.UUID) -> BlacklistEntry:
        return await self._transition(entry_id, status=BlacklistEntryStatus.FAILED)

    async def get_active_by_reference(
        self,
        *,
        guild_id: int,
        type_key: str,
        raw_reference: str,
    ) -> BlacklistEntry | None:
        prefix, number = parse_reference(raw_reference)
        row = await self.client.fetch_one(
            sa.select(blacklist_entries).where(
                blacklist_entries.c.guild_id == guild_id,
                blacklist_entries.c.type_key == type_key,
                blacklist_entries.c.reference_prefix == prefix,
                blacklist_entries.c.number == number,
                blacklist_entries.c.status == BlacklistEntryStatus.ACTIVE.value,
            )
        )
        return self._entry_from_row(row) if row is not None else None

    async def update_active(
        self,
        *,
        entry_id: uuid.UUID,
        guild_id: int,
        type_definition: BlacklistTypeOptions,
        actor_user_id: int,
        raw_pcu_urls: str,
        raw_subject_name: str,
        raw_reason: str,
    ) -> BlacklistEntry:
        profile_ids, pcu_urls = parse_pcu_profile_urls(
            raw_pcu_urls,
            expected_kind=type_definition.profile_kind,
            max_profiles=type_definition.max_profiles,
        )
        subject_name = normalize_subject_name(raw_subject_name)
        name_key = subject_name_key(subject_name)
        reason = raw_reason.strip()
        if not 10 <= len(reason) <= 1_000:
            raise ValueError("La razón debe contener entre 10 y 1000 caracteres.")
        timestamp = datetime.now(UTC)
        async with self.client.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        blacklist_entries.update()
                        .where(
                            blacklist_entries.c.id == entry_id,
                            blacklist_entries.c.guild_id == guild_id,
                            blacklist_entries.c.type_key == type_definition.key,
                            blacklist_entries.c.status == BlacklistEntryStatus.ACTIVE.value,
                        )
                        .values(
                            profile_kind=type_definition.profile_kind.value,
                            pcu_profile_ids=list(profile_ids),
                            pcu_urls=list(pcu_urls),
                            subject_label=type_definition.subject_label.strip(),
                            subject_name=subject_name,
                            subject_name_key=name_key,
                            reason=reason,
                            edited_by_user_id=actor_user_id,
                            edited_at=timestamp,
                            updated_at=timestamp,
                        )
                        .returning(*blacklist_entries.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError("El registro ya no está activo o pertenece a otro tipo.")
        return self._entry_from_row(row)

    async def revoke_active(
        self,
        *,
        entry_id: uuid.UUID,
        guild_id: int,
        type_key: str,
        actor_user_id: int,
    ) -> BlacklistEntry:
        timestamp = datetime.now(UTC)
        async with self.client.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        blacklist_entries.update()
                        .where(
                            blacklist_entries.c.id == entry_id,
                            blacklist_entries.c.guild_id == guild_id,
                            blacklist_entries.c.type_key == type_key,
                            blacklist_entries.c.status == BlacklistEntryStatus.ACTIVE.value,
                        )
                        .values(
                            status=BlacklistEntryStatus.REVOKED.value,
                            revoked_by_user_id=actor_user_id,
                            revoked_at=timestamp,
                            updated_at=timestamp,
                        )
                        .returning(*blacklist_entries.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError("El registro ya no está activo o pertenece a otro tipo.")
        return self._entry_from_row(row)

    async def search(
        self,
        *,
        guild_id: int,
        raw_query: str,
        limit: int = 10,
    ) -> tuple[BlacklistEntry, ...]:
        if not 1 <= limit <= 25:
            raise ValueError("Blacklist search limit must be between 1 and 25")
        profile_kind, profile_id, name_key = parse_lookup(raw_query)
        statement = sa.select(blacklist_entries).where(
            blacklist_entries.c.guild_id == guild_id,
            blacklist_entries.c.status == BlacklistEntryStatus.ACTIVE.value,
        )
        if profile_id is not None:
            statement = statement.where(blacklist_entries.c.pcu_profile_ids.contains([profile_id]))
            if profile_kind is not None:
                statement = statement.where(blacklist_entries.c.profile_kind == profile_kind.value)
        elif name_key is not None:
            statement = statement.where(blacklist_entries.c.subject_name_key.contains(name_key))
        else:
            raise AssertionError("validated blacklist lookup has no search value")
        rows = await self.client.fetch_all(
            statement.order_by(blacklist_entries.c.created_at.desc()).limit(limit)
        )
        return tuple(self._entry_from_row(row) for row in rows)

    async def _transition(
        self,
        entry_id: uuid.UUID,
        *,
        status: BlacklistEntryStatus,
        message_id: int | None = None,
    ) -> BlacklistEntry:
        values: dict[str, object] = {
            "status": status.value,
            "updated_at": datetime.now(UTC),
        }
        if message_id is not None:
            values["message_id"] = message_id
        async with self.client.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        blacklist_entries.update()
                        .where(blacklist_entries.c.id == entry_id)
                        .values(**values)
                        .returning(*blacklist_entries.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError("Blacklist entry no longer exists")
        return self._entry_from_row(row)

    @staticmethod
    def _entry_from_row(row: sa.RowMapping) -> BlacklistEntry:
        return BlacklistEntry(
            id=row["id"],
            guild_id=int(row["guild_id"]),
            type_key=str(row["type_key"]),
            type_name=str(row["type_name"]),
            reference_prefix=str(row["reference_prefix"]),
            number=int(row["number"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            interaction_id=int(row["interaction_id"]),
            author_user_id=int(row["author_user_id"]),
            profile_kind=PcuProfileKind(str(row["profile_kind"])),
            pcu_profile_ids=tuple(int(value) for value in row["pcu_profile_ids"]),
            pcu_urls=tuple(str(value) for value in row["pcu_urls"]),
            subject_label=str(row["subject_label"]),
            subject_name=str(row["subject_name"]),
            subject_name_key=str(row["subject_name_key"]),
            reason=str(row["reason"]),
            status=BlacklistEntryStatus(str(row["status"])),
            edited_by_user_id=(
                int(row["edited_by_user_id"]) if row["edited_by_user_id"] is not None else None
            ),
            edited_at=row["edited_at"],
            revoked_by_user_id=(
                int(row["revoked_by_user_id"]) if row["revoked_by_user_id"] is not None else None
            ),
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
