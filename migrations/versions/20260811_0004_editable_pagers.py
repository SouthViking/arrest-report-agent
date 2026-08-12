"""Publish persona copy for editable pagers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0004"
down_revision: str | None = "20260811_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (("walter_vigil", 20), ("roy_velazco", 27))


def upgrade() -> None:
    _install_persona_revisions(op.get_bind())


def downgrade() -> None:
    _downgrade_personas(op.get_bind())


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
            raise RuntimeError(f"Unexpected packaged revision for {slug}")
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
