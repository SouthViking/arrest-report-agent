"""Add restricted deterministic faction-member analysis."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0047"
down_revision: str | None = "20260823_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "faction_member_analysis"
SOURCE_POLICY_KEY = "supervisor_roles"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "faction_id": 3,
    "base_divisions": ["GNT", "DLM"],
    "inactivity_warning_days": 14,
    "inactivity_critical_days": 30,
    "inactivity_warning_percentage": 20,
    "inactivity_critical_percentage": 30,
}
PERSONA_SCHEMA_VERSION = 45
PERSONA_REVISIONS = (
    ("walter_vigil", 51, 52),
    ("roy_velazco", 58, 59),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Análisis de miembros no disponible",
            "unauthorized": (
                "Esta consulta está reservada al personal de supervisión, mando y dirección autorizado."
            ),
            "unavailable": ("El análisis general de miembros no está habilitado para este servidor."),
            "operation_failed": (
                "No pude preparar el análisis de miembros en este momento. Inténtelo nuevamente más tarde."
            ),
            "intro": ("Resumen general de actividad y distribución del personal de la facción."),
            "no_alerts": ("No se detectaron alertas deterministas en la plantilla actual."),
        },
        "description": "Analiza actividad y distribución de los miembros",
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude analizar la plantilla",
            "unauthorized": (
                "Esto es pa supervisores y mando autorizado, no pa cualquiera q quiera sapear la plantilla."
            ),
            "unavailable": ("No dejaron habilitado el análisis general de miembros en este servidor."),
            "operation_failed": ("No pude armar el análisis de miembros ahora. Prueba de nuevo más rato."),
            "intro": ("Ahí tenís el resumen de actividad y cómo está repartida la plantilla."),
            "no_alerts": (
                "No encontré alertas deterministas en la plantilla actual. Milagro, "
                "está todo bastante ordenado."
            ),
        },
        "description": "Analiza actividad y distribución de los miembros",
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings().all()
    )
    for config_revision in revisions:
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        enabled = is_lssd and _has_restricted_supervisor_roles(
            connection,
            config_revision["id"],
        )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": config_revision["id"],
                "key": FEATURE_KEY,
                "enabled": enabled,
                "payload": json.dumps(LSSD_OPTIONS if enabled else {}),
            },
        )
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)


def _has_restricted_supervisor_roles(
    connection: sa.Connection,
    revision_id: object,
) -> bool:
    return bool(
        connection.execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM guild_access_policies policy "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE policy.config_revision_id = :revision_id "
                "AND policy.policy_key = :source_key "
                "AND policy.mode = 'restricted'"
                ")"
            ),
            {"revision_id": revision_id, "source_key": SOURCE_POLICY_KEY},
        ).scalar_one()
    )


def _install_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
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
        return
    next_id = connection.execute(
        sa.text("SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"),
        {"persona_id": source["persona_id"], "revision": next_revision},
    ).scalar_one_or_none()
    if next_id is None:
        next_id = uuid.uuid4()
        payload = dict(source["payload"])
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        copy = PERSONA_COPY[slug]
        interface = payload["messages"]["interface"]
        interface["faction_member_analysis"] = copy["messages"]
        interface["commands"]["faction_member_analysis_description"] = copy["description"]
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            sa.text(
                "INSERT INTO persona_revisions "
                "(id, persona_id, revision, schema_version, status, payload, checksum, "
                "published_at) VALUES (:id, :persona_id, :revision, :schema_version, "
                "'published', CAST(:payload AS jsonb), :checksum, now())"
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


def _copy_assets(
    connection: sa.Connection,
    previous_id: uuid.UUID,
    next_id: uuid.UUID,
) -> None:
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
                "(id, persona_revision_id, kind, object_key, sha256, content_type, "
                "size_bytes) VALUES (:id, :next, :kind, :object_key, :sha256, "
                ":content_type, :size_bytes)"
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


def _restore_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    rows = (
        connection.execute(
            sa.text(
                "SELECT pr.id, pr.revision FROM persona_revisions pr "
                "JOIN personas p ON p.id = pr.persona_id "
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
        return
    _activate_revision(connection, next_id, previous_id)
    connection.execute(
        sa.text("DELETE FROM persona_revisions WHERE id = :next"),
        {"next": next_id},
    )
