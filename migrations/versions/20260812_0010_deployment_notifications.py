"""Add Railway deployment events, Discord outbox and persona copy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0010"
down_revision: str | None = "20260812_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "deployment_notification_channels"
FEATURE_KEY = "deployment_notifications"
PERSONA_REVISIONS = (
    ("walter_vigil", 24, 25),
    ("roy_velazco", 31, 32),
)
PERSONA_SCHEMA_VERSION = 23
SUGGESTION_COMMAND_KEYS = (
    "suggestion_description",
    "info_suggestions_description",
    "suggestions_description",
    "suggestion_status_description",
)


def upgrade() -> None:
    op.create_table(
        "deployment_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_event_key", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("deployment_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(100), nullable=False),
        sa.Column("environment_id", sa.String(100), nullable=False),
        sa.Column("service_id", sa.String(100), nullable=False),
        sa.Column("source", sa.String(50)),
        sa.Column("branch", sa.String(255)),
        sa.Column("commit_hash", sa.String(64)),
        sa.Column("commit_message", sa.String(500)),
        sa.Column("severity", sa.String(30)),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("ignored_reason", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "event_type IN ('deployed','failed','crashed','oom_killed')",
            name="ck_deployment_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('received','processed','ignored')",
            name="ck_deployment_event_status",
        ),
    )
    op.create_index(
        "ix_deployment_event_status_created",
        "deployment_events",
        ["status", "created_at"],
    )
    op.create_table(
        "deployment_notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deployment_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_kind", sa.String(20), nullable=False),
        sa.Column("notification_key", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("discord_message_id", sa.BigInteger()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "notification_kind IN ('release','incident')",
            name="ck_deployment_notification_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','retry','sent','failed')",
            name="ck_deployment_notification_status",
        ),
    )
    op.create_index(
        "ix_deployment_notification_pending",
        "deployment_notification_deliveries",
        ["status", "next_attempt_at"],
    )
    connection = op.get_bind()
    _install_configuration(connection)
    _install_personas(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )
    op.drop_index(
        "ix_deployment_notification_pending",
        table_name="deployment_notification_deliveries",
    )
    op.drop_table("deployment_notification_deliveries")
    op.drop_index("ix_deployment_event_status_created", table_name="deployment_events")
    op.drop_table("deployment_events")


def _install_configuration(connection: sa.Connection) -> None:
    revision_ids = connection.execute(sa.text("SELECT id FROM guild_config_revisions")).scalars().all()
    for revision_id in revision_ids:
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'channel', 'disabled')"
            ),
            {"id": uuid.uuid4(), "revision_id": revision_id, "key": POLICY_KEY},
        )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, false, 1, '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "revision_id": revision_id, "key": FEATURE_KEY},
        )


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
            int(payload["revision"]) < next_revision
            or int(payload["schema_version"]) < PERSONA_SCHEMA_VERSION
        ):
            raise RuntimeError(f"Packaged persona {slug} does not match deployment migration")
        # Persona files are runtime fixtures and continue evolving after this migration.
        # Reconstruct the schema-23 snapshot instead of requiring the packaged file to
        # remain frozen forever; the following migration will add suggestion copy.
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        interface = payload["messages"]["interface"]
        interface.pop("suggestions", None)
        commands = interface["commands"]
        for command_key in SUGGESTION_COMMAND_KEYS:
            commands.pop(command_key, None)
        help_messages = payload["messages"]["help"]
        help_messages["general_commands"] = "\n".join(
            line
            for line in help_messages.get("general_commands", "").splitlines()
            if "/sugerencia" not in line and "/info-sugerencias" not in line
        )
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
