"""Add the structured logbook, channel policy and persona guidance."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0039"
down_revision: str | None = "20260819_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "logbook"
POLICY_KEY = "logbook_channels"
LSSD_GUILD_ID = 1234674521416663070
LSSD_LOGBOOK_CHANNEL_ID = 1261076248541532240
PERSONA_SCHEMA_VERSION = 43
PERSONA_REVISIONS = (
    ("walter_vigil", 49, 50),
    ("roy_velazco", 56, 57),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Bitácora no disponible",
            "unavailable": "La bitácora estructurada no está habilitada para este servidor.",
            "wrong_channel": "Este registro solo puede utilizarse en los canales de bitácora autorizados.",
            "operation_failed": (
                "No pude registrar la novedad en este momento. Inténtelo nuevamente más tarde."
            ),
            "invalid_amount": (
                "Indique un monto positivo usando números; puede incluir hasta dos decimales."
            ),
            "invalid_files": ("Los adjuntos superan los límites permitidos o contienen un archivo vacío."),
            "setup_description": (
                "Seleccione la clase de novedad y, si corresponde, indique si hubo un "
                "ingreso o un retiro de dinero."
            ),
            "info_intro": (
                "La bitácora permite dejar novedades uniformes, fáciles de consultar y "
                "respaldadas con adjuntos cuando sea necesario."
            ),
            "info_usage": (
                "Ejecute `/bitacora`, seleccione la categoría y complete el formulario "
                "adaptado. El registro se publicará en este mismo canal con una referencia `BIT`."
            ),
            "info_finance": (
                "Si hubo movimiento de dinero, seleccione **Ingreso** o **Retiro** y detalle "
                "el monto y su concepto, origen o destino."
            ),
            "info_attachments": (
                "Puede adjuntar imágenes o documentos de respaldo directamente en el formulario."
            ),
            "info_closing": (
                "Registre hechos concretos y suficientes para que otra persona pueda comprender la novedad."
            ),
        },
        "descriptions": (
            "Registra una novedad estructurada en la bitácora",
            "Explica cómo registrar novedades en la bitácora",
        ),
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude abrir la bitácora",
            "unavailable": "No dejaron habilitada la bitácora estructurada en este server.",
            "wrong_channel": (
                "Esto va en un canal de bitácora autorizado, no donde se te ocurra dejar el registro."
            ),
            "operation_failed": "No pude guardar la novedad ahora. Prueba de nuevo más rato.",
            "invalid_amount": "Pon un monto positivo con números y, si hace falta, hasta dos decimales.",
            "invalid_files": "Uno de los adjuntos está vacío o se pasó de los límites permitidos.",
            "setup_description": (
                "Elige qué pasó y marca si hubo ingreso o retiro de plata; después te pido solo lo necesario."
            ),
            "info_intro": (
                "La bitácora deja las novedades ordenadas pa q después nadie tenga q "
                "descifrar mensajes sueltos."
            ),
            "info_usage": (
                "Usa `/bitacora`, elige la categoría y completa el formulario. La novedad "
                "queda publicada acá con una referencia `BIT`."
            ),
            "info_finance": (
                "Si se movió plata, marca **Ingreso** o **Retiro** y deja claro el monto y "
                "de dónde salió o a dónde fue."
            ),
            "info_attachments": "Podís adjuntar imágenes o documentos de respaldo en el mismo formulario.",
            "info_closing": (
                "Deja hechos claros y concretos; la bitácora no es pa acertijos ni mensajes a medias."
            ),
        },
        "descriptions": (
            "Registra una novedad estructurada en la bitácora",
            "Explica cómo dejar una novedad en la bitácora",
        ),
    },
}


def upgrade() -> None:
    _create_tables()
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
    op.drop_table("logbook_assets")
    op.drop_index("ix_logbook_author_created", table_name="logbook_entries")
    op.drop_index("ix_logbook_guild_created", table_name="logbook_entries")
    op.drop_table("logbook_entries")
    op.drop_table("logbook_counters")


def _create_tables() -> None:
    op.create_table(
        "logbook_counters",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("last_number > 0", name="ck_logbook_counter_positive"),
        sa.ForeignKeyConstraint(["guild_id"], ["discord_guilds.discord_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id"),
    )
    op.create_table(
        "logbook_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("category_key", sa.String(length=30), nullable=False),
        sa.Column("subtype_key", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("money_direction", sa.String(length=20)),
        sa.Column("money_amount", sa.Numeric(precision=16, scale=2)),
        sa.Column("money_detail", sa.String(length=300)),
        sa.Column("status", sa.String(length=20), server_default="publishing", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "category_key IN ('finance','personnel','assignments','equipment',"
            "'orders','evidence','fleet','other')",
            name="ck_logbook_category",
        ),
        sa.CheckConstraint(
            "money_direction IS NULL OR money_direction IN ('income','withdrawal')",
            name="ck_logbook_money_direction",
        ),
        sa.CheckConstraint(
            "(money_direction IS NULL AND money_amount IS NULL) OR "
            "(money_direction IS NOT NULL AND money_amount > 0)",
            name="ck_logbook_money_pair",
        ),
        sa.CheckConstraint(
            "status IN ('publishing','published','failed','voided')", name="ck_logbook_status"
        ),
        sa.CheckConstraint("number > 0", name="ck_logbook_number_positive"),
        sa.CheckConstraint("version > 0", name="ck_logbook_version_positive"),
        sa.ForeignKeyConstraint(["guild_id"], ["discord_guilds.discord_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interaction_id"),
        sa.UniqueConstraint("guild_id", "number", name="uq_logbook_guild_number"),
        sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_logbook_message"),
    )
    op.create_index("ix_logbook_guild_created", "logbook_entries", ["guild_id", "created_at"])
    op.create_index(
        "ix_logbook_author_created", "logbook_entries", ["guild_id", "author_user_id", "created_at"]
    )
    op.create_table(
        "logbook_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_logbook_asset_position"),
        sa.CheckConstraint("size_bytes > 0", name="ck_logbook_asset_size"),
        sa.ForeignKeyConstraint(["entry_id"], ["logbook_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "position", name="uq_logbook_asset_position"),
    )


def _install_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings().all()
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
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) VALUES (:id, :policy_id, :channel, 0)"
                ),
                {"id": uuid.uuid4(), "policy_id": policy_id, "channel": LSSD_LOGBOOK_CHANNEL_ID},
            )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, '{}'::jsonb)"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": config_revision["id"],
                "key": FEATURE_KEY,
                "enabled": enabled,
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
        interface[FEATURE_KEY] = copy["messages"]
        interface["commands"]["logbook_description"] = copy["descriptions"][0]
        interface["commands"]["info_logbook_description"] = copy["descriptions"][1]
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


def _activate_revision(connection: sa.Connection, previous_id: uuid.UUID, next_id: uuid.UUID) -> None:
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
                "SELECT pr.revision, pr.id FROM personas p JOIN persona_revisions pr "
                "ON pr.persona_id = p.id WHERE p.slug = :slug "
                "AND pr.revision IN (:previous, :next)"
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
    connection.execute(sa.text("DELETE FROM persona_revisions WHERE id = :next"), {"next": next_id})
