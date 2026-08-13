"""Add signed GitHub Actions failure notifications."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0018"
down_revision: str | None = "20260813_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "ci_failure_notification_channels"
LSSD_GUILD_ID = 1234674521416663070
LSSD_BOT_GIT_CHANNEL_ID = 1241206587079524442
PERSONA_SCHEMA_VERSION = 28
PERSONA_REVISIONS = (
    ("walter_vigil", 31, 32),
    ("roy_velazco", 38, 39),
)
CI_FAILURE_MESSAGES = {
    "walter_vigil": (
        "La integración continua detectó un problema. Revise la ejecución antes de "
        "considerar este cambio apto para despliegue."
    ),
    "roy_velazco": (
        "quedó la cagá en el CI. revisen la ejecución antes de dar este cambio por bueno."
    ),
}


def upgrade() -> None:
    op.create_table(
        "ci_workflow_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_event_key", sa.String(64), nullable=False, unique=True),
        sa.Column("delivery_id", sa.String(64), nullable=False, unique=True),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_full_name", sa.String(255), nullable=False),
        sa.Column("workflow_id", sa.BigInteger(), nullable=False),
        sa.Column("workflow_name", sa.String(255), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("triggering_event", sa.String(50), nullable=False),
        sa.Column("branch", sa.String(255)),
        sa.Column("commit_hash", sa.String(64), nullable=False),
        sa.Column("commit_message", sa.String(500)),
        sa.Column("actor_login", sa.String(100), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("ignored_reason", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("run_attempt > 0", name="ck_ci_workflow_run_attempt"),
        sa.CheckConstraint(
            "status IN ('received','processed','ignored')",
            name="ck_ci_workflow_event_status",
        ),
        sa.UniqueConstraint(
            "repository_id",
            "run_id",
            "run_attempt",
            name="uq_ci_workflow_run_attempt",
        ),
    )
    op.create_index(
        "ix_ci_workflow_event_status_created",
        "ci_workflow_events",
        ["status", "created_at"],
    )
    op.create_table(
        "ci_notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ci_workflow_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
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
            "status IN ('pending','retry','sent','failed')",
            name="ck_ci_notification_status",
        ),
    )
    op.create_index(
        "ix_ci_notification_pending",
        "ci_notification_deliveries",
        ["status", "next_attempt_at"],
    )
    connection = op.get_bind()
    _install_policy(connection)
    _install_personas(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :policy_key"),
        {"policy_key": POLICY_KEY},
    )
    op.drop_index("ix_ci_notification_pending", table_name="ci_notification_deliveries")
    op.drop_table("ci_notification_deliveries")
    op.drop_index("ix_ci_workflow_event_status_created", table_name="ci_workflow_events")
    op.drop_table("ci_workflow_events")


def _install_policy(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(
            sa.text(
                "SELECT id, guild_id FROM guild_config_revisions revision "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM guild_access_policies policy "
                "WHERE policy.config_revision_id = revision.id "
                "AND policy.policy_key = :policy_key)"
            ),
            {"policy_key": POLICY_KEY},
        )
        .mappings()
        .all()
    )
    for revision in revisions:
        policy_id = uuid.uuid4()
        is_lssd = int(revision["guild_id"]) == LSSD_GUILD_ID
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :policy_key, 'channel', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": revision["id"],
                "policy_key": POLICY_KEY,
                "mode": "restricted" if is_lssd else "disabled",
            },
        )
        if is_lssd:
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, :channel_id, 0)"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "channel_id": LSSD_BOT_GIT_CHANNEL_ID,
                },
            )


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
            payload["messages"]["interface"]["deployments"]["ci_failed_description"] = (
                CI_FAILURE_MESSAGES[slug]
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
            assets = (
                connection.execute(
                    sa.text(
                        "SELECT kind, object_key, sha256, content_type, size_bytes "
                        "FROM persona_assets WHERE persona_revision_id = :previous"
                    ),
                    {"previous": source["revision_id"]},
                )
                .mappings()
                .all()
            )
            for asset in assets:
                connection.execute(
                    sa.text(
                        "INSERT INTO persona_assets "
                        "(id, persona_revision_id, kind, object_key, sha256, "
                        "content_type, size_bytes) VALUES "
                        "(:id, :next, :kind, :object_key, :sha256, :content_type, :size_bytes)"
                    ),
                    {"id": uuid.uuid4(), "next": next_id, **asset},
                )
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
