"""Add stolen-vehicle queries and the configurable notification feed."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0045"
down_revision: str | None = "20260821_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "stolen_vehicles"
POLICY_KEY = "stolen_vehicle_channels"
LSSD_GUILD_ID = 1234674521416663070
LSSD_CHANNEL_IDS = (1539037905014759485,)
PERSONA_SCHEMA_VERSION = 44
PERSONA_REVISIONS = (
    ("walter_vigil", 50, 51),
    ("roy_velazco", 57, 58),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Vehículos robados no disponibles",
            "unauthorized": "Esta consulta está reservada al personal operativo autorizado.",
            "unavailable": (
                "La consulta de vehículos robados no está habilitada para este servidor."
            ),
            "operation_failed": (
                "No pude consultar las denuncias de vehículos robados en este momento. "
                "Inténtelo nuevamente más tarde."
            ),
            "invalid_count": "Indique una cantidad entre 1 y 50 vehículos.",
            "busy": "Ya hay una consulta de vehículos robados en curso para este servidor.",
            "no_results": "No encontré denuncias que coincidan con el estado seleccionado.",
            "other_query": "Solo quien inició la consulta puede cambiar esta página.",
        },
        "description": "Muestra las denuncias recientes de vehículos robados",
        "quick_reference": (
            "• `/vehiculos-robados`: consulta las denuncias recientes de robo de vehículos."
        ),
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude revisar los vehículos",
            "unauthorized": (
                "Esta consulta es pa personal operativo autorizado, no pa cualquiera q quiera "
                "sapear denuncias."
            ),
            "unavailable": (
                "No dejaron habilitada la consulta de vehículos robados en este server."
            ),
            "operation_failed": (
                "No pude revisar las denuncias de vehículos robados ahora. Prueba de nuevo más "
                "rato."
            ),
            "invalid_count": "Pon una cantidad entre 1 y 50 vehículos.",
            "busy": (
                "Ya hay una consulta de vehículos robados andando en este server. Espera q "
                "termine."
            ),
            "no_results": "No encontré denuncias con el estado q elegiste.",
            "other_query": "Esta consulta la abrió otra persona; deja q ella cambie la página.",
        },
        "description": "Muestra las denuncias recientes de vehículos robados",
        "quick_reference": (
            "• `/vehiculos-robados`: muestra las denuncias recientes de vehículos robados."
        ),
    },
}


def upgrade() -> None:
    op.create_table(
        "stolen_vehicle_feed_cursors",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("last_report_id", sa.BigInteger(), nullable=False),
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
        sa.CheckConstraint(
            "last_report_id >= 0",
            name="ck_stolen_vehicle_feed_cursor_nonnegative",
        ),
    )

    connection = op.get_bind()
    _install_configuration(connection)
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)
    op.drop_table("stolen_vehicle_feed_cursors")


def _install_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        policy_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'channel', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": config_revision["id"],
                "key": POLICY_KEY,
                "mode": "restricted" if enabled else "disabled",
            },
        )
        if enabled:
            for position, channel_id in enumerate(LSSD_CHANNEL_IDS):
                connection.execute(
                    sa.text(
                        "INSERT INTO guild_access_policy_members "
                        "(id, policy_id, snowflake_id, position) "
                        "VALUES (:id, :policy_id, :channel_id, :position)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "policy_id": policy_id,
                        "channel_id": channel_id,
                        "position": position,
                    },
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
                "payload": json.dumps({}),
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
        interface["commands"]["stolen_vehicles_description"] = copy["description"]
        help_copy = payload["messages"]["help"]
        quick_lines = [
            line
            for line in help_copy["quick_references"].splitlines()
            if "`/vehiculos-robados`" not in line
        ]
        quick_lines.insert(
            next(
                (
                    index + 1
                    for index, line in enumerate(quick_lines)
                    if "`/recursos`" in line
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
