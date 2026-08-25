"""Restrict LSSD role-request reviews to Command Staff."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0049"
down_revision: str | None = "20260824_0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
# Alembic revisions intentionally freeze the values applied at this point in
# history; runtime and fresh-bootstrap defaults live in config/bootstrap_defaults.py.
COMMAND_STAFF_CHANNEL_ID = 1261076248541532240
PREVIOUS_SUPERVISOR_CHANNEL_ID = 1234678185124298853
COMMAND_STAFF_ROLE_ID = 1234678029662294047
PREVIOUS_REVIEWER_ROLE_ID = 1527777009219010760
REVIEW_CHANNELS_POLICY = "role_request_review_channels"
REVIEWER_ROLES_POLICY = "role_request_reviewer_roles"


def upgrade() -> None:
    _replace_policy_members(
        REVIEW_CHANNELS_POLICY,
        "channel",
        (COMMAND_STAFF_CHANNEL_ID,),
    )
    _replace_policy_members(
        REVIEWER_ROLES_POLICY,
        "role",
        (COMMAND_STAFF_ROLE_ID,),
    )


def downgrade() -> None:
    _replace_policy_members(
        REVIEW_CHANNELS_POLICY,
        "channel",
        (COMMAND_STAFF_CHANNEL_ID, PREVIOUS_SUPERVISOR_CHANNEL_ID),
    )
    _replace_policy_members(
        REVIEWER_ROLES_POLICY,
        "role",
        (PREVIOUS_REVIEWER_ROLE_ID,),
    )


def _replace_policy_members(
    policy_key: str,
    subject_type: str,
    snowflake_ids: tuple[int, ...],
) -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(
            sa.text(
                "SELECT revision.id, revision.guild_id, policy.id AS policy_id "
                "FROM guild_config_revisions revision "
                "LEFT JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = :policy_key"
            ),
            {"policy_key": policy_key},
        )
        .mappings()
        .all()
    )
    for revision in revisions:
        is_lssd = int(revision["guild_id"]) == LSSD_GUILD_ID
        policy_id = revision["policy_id"]
        if policy_id is None:
            policy_id = uuid.uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policies "
                    "(id, config_revision_id, policy_key, subject_type, mode) "
                    "VALUES (:id, :revision_id, :policy_key, :subject_type, :mode)"
                ),
                {
                    "id": policy_id,
                    "revision_id": revision["id"],
                    "policy_key": policy_key,
                    "subject_type": subject_type,
                    "mode": "restricted" if is_lssd else "disabled",
                },
            )
        if not is_lssd:
            continue
        connection.execute(
            sa.text("UPDATE guild_access_policies SET mode = 'restricted' WHERE id = :id"),
            {"id": policy_id},
        )
        connection.execute(
            sa.text("DELETE FROM guild_access_policy_members WHERE policy_id = :policy_id"),
            {"policy_id": policy_id},
        )
        for position, snowflake_id in enumerate(snowflake_ids):
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, :snowflake_id, :position)"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "snowflake_id": snowflake_id,
                    "position": position,
                },
            )
