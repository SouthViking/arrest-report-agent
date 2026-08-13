"""Add the private deterministic suggestion inbox and persona copy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0011"
down_revision: str | None = "20260812_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 25, 26),
    ("roy_velazco", 32, 33),
)
PERSONA_SCHEMA_VERSION = 24


def upgrade() -> None:
    op.create_table(
        "suggestion_counters",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("last_number > 0", name="ck_suggestion_counter_positive"),
    )
    op.create_table(
        "suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("category_key", sa.String(30), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "category_key IN ('automation','procedure','documentation','experience','correction','other')",
            name="ck_suggestion_category",
        ),
        sa.CheckConstraint("status IN ('pending','archived')", name="ck_suggestion_status"),
        sa.CheckConstraint("number > 0", name="ck_suggestion_number_positive"),
        sa.CheckConstraint("version > 0", name="ck_suggestion_version_positive"),
        sa.UniqueConstraint("guild_id", "number", name="uq_suggestion_guild_number"),
    )
    op.create_index(
        "ix_suggestion_guild_created",
        "suggestions",
        ["guild_id", "created_at"],
    )
    op.create_index(
        "ix_suggestion_author_pending",
        "suggestions",
        ["guild_id", "author_user_id", "status", "created_at"],
    )
    op.create_table(
        "suggestion_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "suggestion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suggestions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ('submitted','status_changed')",
            name="ck_suggestion_event_type",
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('pending','archived')",
            name="ck_suggestion_event_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('pending','archived')",
            name="ck_suggestion_event_to_status",
        ),
    )
    op.create_index(
        "ix_suggestion_event_history",
        "suggestion_events",
        ["suggestion_id", "created_at"],
    )
    _install_personas(op.get_bind())


def downgrade() -> None:
    _downgrade_personas(op.get_bind())
    op.drop_index("ix_suggestion_event_history", table_name="suggestion_events")
    op.drop_table("suggestion_events")
    op.drop_index("ix_suggestion_author_pending", table_name="suggestions")
    op.drop_index("ix_suggestion_guild_created", table_name="suggestions")
    op.drop_table("suggestions")
    op.drop_table("suggestion_counters")


def _install_personas(connection: sa.Connection) -> None:
    persona_dir = Path(__file__).resolve().parents[2] / "src" / "arrest_report_agent" / "config" / "personas"
    if not persona_dir.exists():
        return
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        source = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, pr.id AS revision_id "
                    "FROM personas p JOIN persona_revisions pr ON pr.persona_id = p.id "
                    "WHERE p.slug = :slug AND pr.revision = :revision"
                ),
                {"slug": slug, "revision": previous_revision},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            continue
        payload = json.loads((persona_dir / f"{slug}.json").read_text(encoding="utf-8"))
        if (
            int(payload["revision"]) != next_revision
            or int(payload["schema_version"]) != PERSONA_SCHEMA_VERSION
        ):
            raise RuntimeError(f"Packaged persona {slug} does not match suggestion migration")
        existing = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": source["persona_id"], "revision": next_revision},
        ).scalar_one_or_none()
        revision_id = existing or uuid.uuid4()
        if existing is None:
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            connection.execute(
                sa.text(
                    "INSERT INTO persona_revisions "
                    "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                    "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                    "CAST(:payload AS jsonb), :checksum, now())"
                ),
                {
                    "id": revision_id,
                    "persona_id": source["persona_id"],
                    "revision": next_revision,
                    "schema_version": PERSONA_SCHEMA_VERSION,
                    "payload": serialized.decode(),
                    "checksum": hashlib.sha256(serialized).hexdigest(),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO persona_assets "
                    "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                    "SELECT :id, :next_revision, kind, object_key, sha256, content_type, size_bytes "
                    "FROM persona_assets WHERE persona_revision_id = :previous_revision"
                ),
                {
                    "id": uuid.uuid4(),
                    "next_revision": revision_id,
                    "previous_revision": source["revision_id"],
                },
            )
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
        )


def _downgrade_personas(connection: sa.Connection) -> None:
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        rows = (
            connection.execute(
                sa.text(
                    "SELECT pr.revision, pr.id FROM personas p "
                    "JOIN persona_revisions pr ON pr.persona_id = p.id "
                    "WHERE p.slug = :slug AND pr.revision IN (:previous, :next)"
                ),
                {"slug": slug, "previous": previous_revision, "next": next_revision},
            )
            .mappings()
            .all()
        )
        ids = {int(row["revision"]): row["id"] for row in rows}
        previous_id, next_id = ids.get(previous_revision), ids.get(next_revision)
        if previous_id is None or next_id is None:
            continue
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :next"
            ),
            {"previous": previous_id, "next": next_id},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :next"
            ),
            {"previous": previous_id, "next": next_id},
        )
        connection.execute(
            sa.text("DELETE FROM persona_revisions WHERE id = :next"),
            {"next": next_id},
        )
