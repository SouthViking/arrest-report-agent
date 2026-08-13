"""Add explicit agent user denials and persona-aware blocked notices."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0017"
down_revision: str | None = "20260813_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "agent_blocked_users"
PERSONA_SCHEMA_VERSION = 27
PERSONA_REVISIONS = (
    ("walter_vigil", 30, 31),
    ("roy_velazco", 37, 38),
)
BLOCKED_MESSAGES = {
    "walter_vigil": [
        "⛔ **Acceso bloqueado.** Su usuario figura expresamente en la lista de exclusión "
        "del agente. Consulte con un administrador si considera que se trata de un error.",
        "⛔ **No puedo atender esta solicitud.** Su acceso al agente fue bloqueado de forma "
        "explícita; pertenecer a un rol autorizado no anula esa restricción.",
        "⛔ **Solicitud denegada.** Esta cuenta está excluida del uso del agente. Un "
        "administrador deberá retirar el bloqueo antes de que pueda continuar.",
    ],
    "roy_velazco": [
        "⛔ **ni lo intentís.** estai en la lista de bloqueados y tus roles no te salvan acá. "
        "habla con un admin si creís q fue un error.",
        "⛔ **hasta aquí llegaste, campeón.** tu usuario está bloqueado pa usar el agente; "
        "mientras no te saquen de la lista no hay conversa.",
        "⛔ **no po.** aparecís bloqueado de forma explícita, así q da lo mismo cuántos "
        "roles tengai. arregla eso con un admin primero.",
    ],
}


def upgrade() -> None:
    connection = op.get_bind()
    _add_policy(connection)
    op.execute("ALTER TABLE persona_assets DROP CONSTRAINT IF EXISTS uq_persona_asset_key")
    _install_personas(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    op.create_unique_constraint("uq_persona_asset_key", "persona_assets", ["object_key"])
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :policy_key"),
        {"policy_key": POLICY_KEY},
    )


def _add_policy(connection: sa.Connection) -> None:
    revision_ids = connection.execute(
        sa.text(
            "SELECT revision.id FROM guild_config_revisions revision "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM guild_access_policies policy "
            "WHERE policy.config_revision_id = revision.id AND policy.policy_key = :policy_key)"
        ),
        {"policy_key": POLICY_KEY},
    ).scalars()
    policy_table = sa.table(
        "guild_access_policies",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_key", sa.String()),
        sa.column("subject_type", sa.String()),
        sa.column("mode", sa.String()),
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "config_revision_id": revision_id,
            "policy_key": POLICY_KEY,
            "subject_type": "user",
            "mode": "disabled",
        }
        for revision_id in revision_ids
    ]
    if rows:
        op.bulk_insert(policy_table, rows)


def _install_personas(connection: sa.Connection) -> None:
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        source = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, pr.id AS revision_id, pr.payload "
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
        next_id = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions "
                "WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": source["persona_id"], "revision": next_revision},
        ).scalar_one_or_none()
        if next_id is None:
            next_id = uuid.uuid4()
            payload = dict(source["payload"])
            payload["revision"] = next_revision
            payload["schema_version"] = PERSONA_SCHEMA_VERSION
            payload["messages"]["access"]["blocked_user_messages"] = BLOCKED_MESSAGES[slug]
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
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next "
                "WHERE persona_revision_id = :previous"
            ),
            {"next": next_id, "previous": source["revision_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next "
                "WHERE persona_revision_id = :previous"
            ),
            {"next": next_id, "previous": source["revision_id"]},
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
                "VALUES (:id, :revision_id, :kind, :object_key, :sha256, :content_type, :size_bytes)"
            ),
            {"id": uuid.uuid4(), "revision_id": next_id, **asset},
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
        connection.execute(sa.text("DELETE FROM persona_revisions WHERE id = :next"), {"next": next_id})
