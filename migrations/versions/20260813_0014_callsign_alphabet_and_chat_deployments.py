"""Widen callsign suffixes and notify deployments in the LSSD chat channel."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0014"
down_revision: str | None = "20260813_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
LSSD_CHAT_CHANNEL_ID = 1234675609536430152
DEPLOYMENT_POLICY_KEY = "deployment_notification_channels"
PERSONA_REVISIONS = (
    ("walter_vigil", 28, 29),
    ("roy_velazco", 35, 36),
)
PERSONA_SCHEMA_VERSION = 26
CALLSIGN_MESSAGES = {
    "walter_vigil": (
        "Utilice entre dos y tres números seguidos de una letra de A a Z. "
        "Ejemplos: **280L**, **60F** o **64A**."
    ),
    "roy_velazco": (
        "Ese callsign no sirve. Pon dos o tres números y termina en una letra de la A a la Z; "
        "por ejemplo **280L**, **60F** o **64A**."
    ),
}


def upgrade() -> None:
    connection = op.get_bind()
    _add_chat_deployment_target(connection)
    _install_personas(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    _remove_chat_deployment_target(connection)


def _add_chat_deployment_target(connection: sa.Connection) -> None:
    policy = (
        connection.execute(
            sa.text(
                "SELECT gap.id "
                "FROM discord_guilds dg "
                "JOIN guild_access_policies gap "
                "ON gap.config_revision_id = dg.active_config_revision_id "
                "WHERE dg.discord_id = :guild_id "
                "AND gap.policy_key = :policy_key "
                "AND gap.subject_type = 'channel' "
                "AND gap.mode = 'restricted'"
            ),
            {"guild_id": LSSD_GUILD_ID, "policy_key": DEPLOYMENT_POLICY_KEY},
        )
        .mappings()
        .one_or_none()
    )
    if policy is None:
        return
    connection.execute(
        sa.text(
            "INSERT INTO guild_access_policy_members (id, policy_id, snowflake_id, position) "
            "SELECT :id, :policy_id, :channel_id, "
            "COALESCE(MAX(position), -1) + 1 "
            "FROM guild_access_policy_members WHERE policy_id = :policy_id "
            "ON CONFLICT (policy_id, snowflake_id) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "policy_id": policy["id"],
            "channel_id": LSSD_CHAT_CHANNEL_ID,
        },
    )


def _remove_chat_deployment_target(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "DELETE FROM guild_access_policy_members gapm "
            "USING guild_access_policies gap, guild_config_revisions gcr "
            "WHERE gapm.policy_id = gap.id "
            "AND gap.config_revision_id = gcr.id "
            "AND gcr.guild_id = :guild_id "
            "AND gap.policy_key = :policy_key "
            "AND gapm.snowflake_id = :channel_id"
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "policy_key": DEPLOYMENT_POLICY_KEY,
            "channel_id": LSSD_CHAT_CHANNEL_ID,
        },
    )


def _install_personas(connection: sa.Connection) -> None:
    persona_dir = Path(__file__).resolve().parents[2] / "src" / "arrest_report_agent" / "config" / "personas"
    if not persona_dir.exists():
        return
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
        existing = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions "
                "WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": source["persona_id"], "revision": next_revision},
        ).scalar_one_or_none()
        revision_id = existing or uuid.uuid4()
        if existing is None:
            payload = dict(source["payload"])
            payload["revision"] = next_revision
            payload["schema_version"] = PERSONA_SCHEMA_VERSION
            payload["messages"]["interface"]["callsigns"]["invalid_format"] = (
                CALLSIGN_MESSAGES[slug]
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
