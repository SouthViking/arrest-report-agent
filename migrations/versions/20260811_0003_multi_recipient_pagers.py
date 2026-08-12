"""Allow multiple pager recipients and refresh persona pager copy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260811_0003"
down_revision: str | None = "20260811_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (("walter_vigil", 19), ("roy_velazco", 26))


def upgrade() -> None:
    op.create_table(
        "pager_recipient_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pager_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pagers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_pager_recipient_position"),
        sa.UniqueConstraint("pager_id", "role_id", name="uq_pager_recipient_role"),
        sa.UniqueConstraint("pager_id", "position", name="uq_pager_recipient_position"),
    )
    connection = op.get_bind()
    existing = connection.execute(sa.text("SELECT id, recipient_role_id FROM pagers")).mappings().all()
    recipient_table = sa.table(
        "pager_recipient_roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("pager_id", postgresql.UUID(as_uuid=True)),
        sa.column("role_id", sa.BigInteger()),
        sa.column("position", sa.Integer()),
    )
    if existing:
        op.bulk_insert(
            recipient_table,
            [
                {
                    "id": uuid.uuid4(),
                    "pager_id": row["id"],
                    "role_id": row["recipient_role_id"],
                    "position": 0,
                }
                for row in existing
            ],
        )
    op.drop_column("pagers", "recipient_role_id")
    _install_persona_revisions(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    op.add_column("pagers", sa.Column("recipient_role_id", sa.BigInteger(), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE pagers AS p SET recipient_role_id = ("
            "SELECT role_id FROM pager_recipient_roles AS pr "
            "WHERE pr.pager_id = p.id ORDER BY pr.position LIMIT 1)"
        )
    )
    op.alter_column("pagers", "recipient_role_id", nullable=False)
    op.drop_table("pager_recipient_roles")


def _install_persona_revisions(connection: sa.Connection) -> None:
    persona_dir = Path(__file__).resolve().parents[2] / "src" / "arrest_report_agent" / "config" / "personas"
    if not persona_dir.exists():
        return
    for slug, expected_revision in PERSONA_REVISIONS:
        persona = (
            connection.execute(sa.text("SELECT id FROM personas WHERE slug = :slug"), {"slug": slug})
            .mappings()
            .one_or_none()
        )
        if persona is None:
            continue
        payload = json.loads((persona_dir / f"{slug}.json").read_text(encoding="utf-8"))
        if payload["revision"] != expected_revision:
            continue
        existing = (
            connection.execute(
                sa.text(
                    "SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"
                ),
                {"persona_id": persona["id"], "revision": expected_revision},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            revision_id = existing["id"]
        else:
            previous_asset = (
                connection.execute(
                    sa.text(
                        "SELECT pa.object_key, pa.sha256, pa.content_type, pa.size_bytes "
                        "FROM persona_revisions pr "
                        "JOIN persona_assets pa ON pa.persona_revision_id = pr.id "
                        "WHERE pr.persona_id = :persona_id AND pa.kind = 'avatar' "
                        "ORDER BY pr.revision DESC LIMIT 1"
                    ),
                    {"persona_id": persona["id"]},
                )
                .mappings()
                .one()
            )
            revision_id = uuid.uuid4()
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
                    "persona_id": persona["id"],
                    "revision": expected_revision,
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
                    "revision_id": revision_id,
                    "object_key": previous_asset["object_key"],
                    "sha256": previous_asset["sha256"],
                    "content_type": previous_asset["content_type"],
                    "size_bytes": previous_asset["size_bytes"],
                },
            )
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :revision_id "
                "WHERE config_revision_id IN ("
                "SELECT id FROM guild_config_revisions WHERE status IN ('published','draft')) "
                "AND persona_revision_id IN ("
                "SELECT id FROM persona_revisions WHERE persona_id = :persona_id)"
            ),
            {"revision_id": revision_id, "persona_id": persona["id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :revision_id "
                "WHERE persona_revision_id IN ("
                "SELECT id FROM persona_revisions WHERE persona_id = :persona_id)"
            ),
            {"revision_id": revision_id, "persona_id": persona["id"]},
        )


def _downgrade_personas(connection: sa.Connection) -> None:
    for slug, revision_number in PERSONA_REVISIONS:
        row = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, current.id AS current_id, previous.id AS previous_id "
                    "FROM personas p "
                    "JOIN persona_revisions current ON current.persona_id = p.id "
                    "AND current.revision = :revision "
                    "JOIN LATERAL (SELECT id FROM persona_revisions "
                    "WHERE persona_id = p.id AND revision < :revision "
                    "ORDER BY revision DESC LIMIT 1) previous ON true "
                    "WHERE p.slug = :slug"
                ),
                {"slug": slug, "revision": revision_number},
            )
            .mappings()
            .one_or_none()
        )
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
