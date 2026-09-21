"""Accept any well-formed FTP request date and time."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0074"
down_revision: str | None = "20260921_0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 47
PERSONA_REVISIONS = {
    "walter_vigil": (56, 57),
    "roy_velazco": (64, 65),
}
INVALID_SCHEDULE_COPY = {
    "walter_vigil": (
        "Indique una fecha y hora válidas, con el formato DD/MM/AAAA y HH:MM."
    ),
    "roy_velazco": (
        "Esa fecha u hora no sirve. Usa DD/MM/AAAA y HH:MM, no jeroglíficos."
    ),
}


def upgrade() -> None:
    connection = op.get_bind()
    for slug, revisions in PERSONA_REVISIONS.items():
        _install_persona_revision(connection, slug, *revisions)


def downgrade() -> None:
    connection = op.get_bind()
    for slug, revisions in PERSONA_REVISIONS.items():
        _remove_persona_revision(connection, slug, *revisions)


def _install_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    source = (
        connection.execute(
            sa.text(
                "SELECT persona.id AS persona_id, revision.id AS revision_id, revision.payload "
                "FROM personas persona "
                "JOIN persona_revisions revision ON revision.persona_id = persona.id "
                "WHERE persona.slug = :slug AND revision.revision = :revision"
            ),
            {"slug": slug, "revision": previous_revision},
        )
        .mappings()
        .one_or_none()
    )
    if source is None:
        return

    next_id = connection.execute(
        sa.text(
            "SELECT id FROM persona_revisions "
            "WHERE persona_id = :persona_id AND revision = :revision"
        ),
        {"persona_id": source["persona_id"], "revision": next_revision},
    ).scalar_one_or_none()
    if next_id is None:
        next_id = uuid.uuid4()
        payload = copy.deepcopy(dict(source["payload"]))
        payload["revision"] = next_revision
        payload["messages"]["interface"]["ftp_requests"]["invalid_schedule"] = (
            INVALID_SCHEDULE_COPY[slug]
        )
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            sa.text(
                "INSERT INTO persona_revisions "
                "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                "CAST(:payload AS jsonb), :checksum, now())"
            ),
            {
                "id": next_id,
                "persona_id": source["persona_id"],
                "revision": next_revision,
                "schema_version": PERSONA_SCHEMA_VERSION,
                "payload": serialized.decode(),
                "checksum": hashlib.sha256(serialized).hexdigest(),
            },
        )
        _copy_assets(connection, source["revision_id"], next_id)
    _activate_revision(connection, source["revision_id"], next_id)


def _remove_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    rows = (
        connection.execute(
            sa.text(
                "SELECT revision.revision, revision.id FROM personas persona "
                "JOIN persona_revisions revision ON revision.persona_id = persona.id "
                "WHERE persona.slug = :slug AND revision.revision IN (:previous, :next)"
            ),
            {"slug": slug, "previous": previous_revision, "next": next_revision},
        )
        .mappings()
        .all()
    )
    ids = {int(row["revision"]): row["id"] for row in rows}
    previous_id = ids.get(previous_revision)
    next_id = ids.get(next_revision)
    if previous_id is None or next_id is None:
        return
    _activate_revision(connection, next_id, previous_id)
    connection.execute(
        sa.text("DELETE FROM persona_revisions WHERE id = :revision_id"),
        {"revision_id": next_id},
    )


def _copy_assets(connection: sa.Connection, previous_id: uuid.UUID, next_id: uuid.UUID) -> None:
    assets = (
        connection.execute(
            sa.text(
                "SELECT kind, object_key, sha256, content_type, size_bytes "
                "FROM persona_assets WHERE persona_revision_id = :previous"
            ),
            {"previous": previous_id},
        )
        .mappings()
        .all()
    )
    for asset in assets:
        connection.execute(
            sa.text(
                "INSERT INTO persona_assets "
                "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                "VALUES (:id, :next, :kind, :object_key, :sha256, :content_type, :size_bytes)"
            ),
            {"id": uuid.uuid4(), "next": next_id, **asset},
        )


def _activate_revision(
    connection: sa.Connection,
    previous_id: uuid.UUID,
    next_id: uuid.UUID,
) -> None:
    connection.execute(
        sa.text(
            "UPDATE guild_persona_assignments SET persona_revision_id = :next "
            "WHERE persona_revision_id = :previous"
        ),
        {"next": next_id, "previous": previous_id},
    )
    connection.execute(
        sa.text(
            "UPDATE bot_application_profile SET persona_revision_id = :next "
            "WHERE persona_revision_id = :previous"
        ),
        {"next": next_id, "previous": previous_id},
    )
