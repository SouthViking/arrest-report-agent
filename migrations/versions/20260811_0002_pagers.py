"""Add deterministic pager persistence and configuration policies."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260811_0002"
down_revision: str | None = "20260811_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAGER_POLICIES = (
    ("pager_channels", "channel"),
    ("pager_creator_roles", "role"),
    ("pager_recipient_roles", "role"),
    ("pager_closer_roles", "role"),
)
LSSD_GUILD_ID = 1234674521416663070


def upgrade() -> None:
    op.drop_constraint("uq_persona_asset_key", "persona_assets", type_="unique")
    op.create_table(
        "pagers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("creator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_callsign", sa.String(40), nullable=False),
        sa.Column("recipient_role_id", sa.BigInteger(), nullable=False),
        sa.Column("location", sa.String(200), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resolution", sa.String(1000), nullable=True),
        sa.Column("closed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('publishing','active','concluded','cancelled','failed')",
            name="ck_pager_status",
        ),
        sa.CheckConstraint("number BETWEEN 1 AND 99999", name="ck_pager_number"),
        sa.UniqueConstraint("guild_id", "number", name="uq_pager_guild_number"),
        sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_pager_message"),
    )
    op.create_index(
        "ix_pager_guild_status_created",
        "pagers",
        ["guild_id", "status", "created_at"],
    )
    op.create_index("ix_pager_creator", "pagers", ["guild_id", "creator_user_id", "created_at"])
    op.create_table(
        "pager_responders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pager_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pagers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("callsign", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("pager_id", "user_id", name="uq_pager_responder"),
    )
    op.create_index("ix_pager_responder_user", "pager_responders", ["user_id", "updated_at"])

    connection = op.get_bind()
    revision_ids = connection.execute(sa.text("SELECT id FROM guild_config_revisions")).scalars().all()
    policy_table = sa.table(
        "guild_access_policies",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_key", sa.String()),
        sa.column("subject_type", sa.String()),
        sa.column("mode", sa.String()),
    )
    feature_table = sa.table(
        "guild_feature_settings",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("feature_key", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("schema_version", sa.Integer()),
        sa.column("payload", postgresql.JSONB()),
    )
    revision_table = sa.table(
        "guild_config_revisions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("guild_id", sa.BigInteger()),
    )
    guild_by_revision = dict(
        connection.execute(sa.select(revision_table.c.id, revision_table.c.guild_id)).all()
    )
    if revision_ids:
        op.bulk_insert(
            policy_table,
            [
                {
                    "id": uuid.uuid4(),
                    "config_revision_id": revision_id,
                    "policy_key": key,
                    "subject_type": subject_type,
                    "mode": "disabled",
                }
                for revision_id in revision_ids
                for key, subject_type in PAGER_POLICIES
            ],
        )

        op.bulk_insert(
            feature_table,
            [
                {
                    "id": uuid.uuid4(),
                    "config_revision_id": revision_id,
                    "feature_key": "pagers",
                    "enabled": False,
                    "schema_version": 1,
                    "payload": {
                        "starting_number": 92
                        if guild_by_revision.get(revision_id) == LSSD_GUILD_ID
                        else 1
                    },
                }
                for revision_id in revision_ids
            ],
        )
    _install_persona_revisions(connection)


def downgrade() -> None:
    _downgrade_personas(op.get_bind())
    op.create_unique_constraint("uq_persona_asset_key", "persona_assets", ["object_key"])
    op.execute("DELETE FROM guild_feature_settings WHERE feature_key = 'pagers'")
    op.execute(
        "DELETE FROM guild_access_policies WHERE policy_key IN "
        "('pager_channels','pager_creator_roles','pager_recipient_roles','pager_closer_roles')"
    )
    op.drop_index("ix_pager_responder_user", table_name="pager_responders")
    op.drop_table("pager_responders")
    op.drop_index("ix_pager_creator", table_name="pagers")
    op.drop_index("ix_pager_guild_status_created", table_name="pagers")
    op.drop_table("pagers")


def _install_persona_revisions(connection: sa.Connection) -> None:
    """Publish packaged schema-v16 personas while reusing checksum-addressed avatars."""

    from pathlib import Path

    persona_dir = Path(__file__).resolve().parents[2] / "src" / "arrest_report_agent" / "config" / "personas"
    if not persona_dir.exists():
        return
    for slug in ("walter_vigil", "roy_velazco"):
        payload = json.loads((persona_dir / f"{slug}.json").read_text(encoding="utf-8"))
        persona = connection.execute(
            sa.text("SELECT id FROM personas WHERE slug = :slug"), {"slug": slug}
        ).mappings().one_or_none()
        if persona is None:
            continue
        existing = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": persona["id"], "revision": payload["revision"]},
        ).mappings().one_or_none()
        if existing is not None:
            new_revision_id = existing["id"]
        else:
            previous = connection.execute(
                sa.text(
                    "SELECT pr.id, pa.object_key, pa.sha256, pa.content_type, pa.size_bytes "
                    "FROM persona_revisions pr JOIN persona_assets pa ON pa.persona_revision_id = pr.id "
                    "WHERE pr.persona_id = :persona_id AND pa.kind = 'avatar' "
                    "ORDER BY pr.revision DESC LIMIT 1"
                ),
                {"persona_id": persona["id"]},
            ).mappings().one()
            new_revision_id = uuid.uuid4()
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            connection.execute(
                sa.text(
                    "INSERT INTO persona_revisions "
                    "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                    "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                    "CAST(:payload AS jsonb), :checksum, now())"
                ),
                {
                    "id": new_revision_id,
                    "persona_id": persona["id"],
                    "revision": payload["revision"],
                    "schema_version": payload["schema_version"],
                    "payload": serialized.decode(),
                    "checksum": hashlib.sha256(serialized).hexdigest(),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO persona_assets "
                    "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                    "VALUES (:id, :revision_id, 'avatar', :object_key, :sha256, :content_type, :size_bytes)"
                ),
                {
                    "id": uuid.uuid4(),
                    "revision_id": new_revision_id,
                    "object_key": previous["object_key"],
                    "sha256": previous["sha256"],
                    "content_type": previous["content_type"],
                    "size_bytes": previous["size_bytes"],
                },
            )
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :new_revision_id "
                "WHERE config_revision_id IN "
                "(SELECT id FROM guild_config_revisions WHERE status IN ('published','draft')) "
                "AND persona_revision_id IN "
                "(SELECT id FROM persona_revisions WHERE persona_id = :persona_id)"
            ),
            {"new_revision_id": new_revision_id, "persona_id": persona["id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :new_revision_id "
                "WHERE persona_revision_id IN "
                "(SELECT id FROM persona_revisions WHERE persona_id = :persona_id)"
            ),
            {"new_revision_id": new_revision_id, "persona_id": persona["id"]},
        )


def _downgrade_personas(connection: sa.Connection) -> None:
    for slug, revision in (("walter_vigil", 18), ("roy_velazco", 25)):
        row = connection.execute(
            sa.text(
                "SELECT p.id AS persona_id, current.id AS current_id, previous.id AS previous_id "
                "FROM personas p "
                "JOIN persona_revisions current ON current.persona_id = p.id "
                "AND current.revision = :revision "
                "JOIN LATERAL (SELECT id FROM persona_revisions "
                "WHERE persona_id = p.id AND revision < :revision ORDER BY revision DESC LIMIT 1) "
                "previous ON true "
                "WHERE p.slug = :slug"
            ),
            {"slug": slug, "revision": revision},
        ).mappings().one_or_none()
        if row is None:
            continue
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :current"
            ),
            {"previous": row["previous_id"], "current": row["current_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :current"
            ),
            {"previous": row["previous_id"], "current": row["current_id"]},
        )
        connection.execute(
            sa.text("DELETE FROM persona_revisions WHERE id = :current"),
            {"current": row["current_id"]},
        )
