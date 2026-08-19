"""Add the operational resources panel and its persona copy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0038"
down_revision: str | None = "20260819_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "operational_resources"
LSSD_GUILD_ID = 1234674521416663070
PERSONA_SCHEMA_VERSION = 42
PERSONA_REVISIONS = (
    ("walter_vigil", 48, 49),
    ("roy_velazco", 55, 56),
)
LSSD_OPTIONS = {
    "faction_id": 3,
    "game_server_address": "play.sarp.es:7777",
    "pcu_url": "https://ucp.sarp.es/app/ucp/home",
    "forum_url": (
        "https://forum.sarp.es/index.php?/forum/199-foro-los-santos-county-sheriffs-department/"
    ),
    "manual_url": (
        "https://docs.google.com/document/d/1DrhSgzPbdLoe0ahFTVrNPb_zBPuhVeatxW5Hs8ECMHE/"
        "edit?pli=1&tab=t.0#heading=h.thzh3oj2x0rk"
    ),
    "penal_code_url": "https://ucp.sarp.es/app/ucp/penalcode",
}
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Recursos operativos no disponibles",
            "unauthorized": "Este panel está reservado al personal operativo autorizado.",
            "unavailable": "Los recursos operativos no están habilitados para este servidor.",
            "operation_failed": (
                "No pude preparar los recursos operativos en este momento. Inténtelo "
                "nuevamente más tarde."
            ),
            "intro": "Accesos y estado actual para iniciar el servicio.",
            "connected_reference": (
                "Para revisar quiénes están conectados y en servicio, utilice `/conectados`."
            ),
        },
        "description": "Muestra accesos y estado operativo de la facción",
        "quick_reference": "• `/recursos`: reúne accesos y estado operativo.",
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude cargar los recursos",
            "unauthorized": (
                "Este panel es pa personal operativo, no pa cualquiera q venga a sapear accesos."
            ),
            "unavailable": "No dejaron habilitados los recursos operativos en este server.",
            "operation_failed": "No pude armar el panel ahora. Prueba de nuevo más rato.",
            "intro": (
                "Ahí tenís lo necesario pa entrar en servicio y revisar el estado actual."
            ),
            "connected_reference": (
                "Si querís ver el detalle de quién está conectado y de servicio, "
                "usa `/conectados`."
            ),
        },
        "description": "Muestra accesos y estado operativo de la facción",
        "quick_reference": "• `/recursos`: junta accesos y estado operativo.",
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    _install_feature_configuration(connection)
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


def _install_feature_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
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
        copy = PERSONA_COPY[slug]
        interface = payload["messages"]["interface"]
        interface[FEATURE_KEY] = copy["messages"]
        interface["commands"]["resources_description"] = copy["description"]
        help_copy = payload["messages"]["help"]
        quick_lines = [
            line
            for line in help_copy["quick_references"].splitlines()
            if "`/recursos`" not in line
        ]
        quick_lines.insert(
            next(
                (
                    index + 1
                    for index, line in enumerate(quick_lines)
                    if "`/conectados`" in line
                ),
                len(quick_lines),
            ),
            copy["quick_reference"],
        )
        help_copy["quick_references"] = "\n".join(quick_lines)
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


def _restore_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
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
        return
    _activate_revision(connection, next_id, previous_id)
    connection.execute(
        sa.text("DELETE FROM persona_revisions WHERE id = :next"),
        {"next": next_id},
    )
