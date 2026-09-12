"""Support account-level blacklists with multiple PCU profiles."""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0064"
down_revision: str | None = "20260912_0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "blacklists"
LSSD_GUILD_ID = 1234674521416663070
FACTION_CHANNEL_ID = 1383926305136971906
FACTION_TYPE = {
    "key": "faction",
    "display_name": "Facción",
    "reference_prefix": "FAC",
    "channel_ids": [FACTION_CHANNEL_ID],
    "profile_kind": "account",
    "max_profiles": 5,
    "subject_label": "Persona",
    "enabled": True,
}


def upgrade() -> None:
    op.drop_index("ix_blacklist_guild_pcu", table_name="blacklist_entries")
    op.drop_index("ix_blacklist_guild_character", table_name="blacklist_entries")
    op.drop_constraint(
        "ck_blacklist_pcu_profile_positive",
        "blacklist_entries",
        type_="check",
    )
    op.add_column(
        "blacklist_entries",
        sa.Column(
            "profile_kind",
            sa.String(length=20),
            nullable=False,
            server_default="character",
        ),
    )
    op.add_column(
        "blacklist_entries",
        sa.Column("pcu_profile_ids", postgresql.ARRAY(sa.BigInteger()), nullable=True),
    )
    op.add_column(
        "blacklist_entries",
        sa.Column(
            "pcu_urls",
            postgresql.ARRAY(sa.String(length=200)),
            nullable=True,
        ),
    )
    op.add_column(
        "blacklist_entries",
        sa.Column(
            "subject_label",
            sa.String(length=30),
            nullable=False,
            server_default="Personaje",
        ),
    )
    op.execute(
        "UPDATE blacklist_entries "
        "SET pcu_profile_ids = ARRAY[pcu_profile_id]::bigint[], "
        "pcu_urls = ARRAY[pcu_url]::varchar(200)[]"
    )
    op.alter_column(
        "blacklist_entries",
        "pcu_profile_ids",
        existing_type=postgresql.ARRAY(sa.BigInteger()),
        nullable=False,
    )
    op.alter_column(
        "blacklist_entries",
        "pcu_urls",
        existing_type=postgresql.ARRAY(sa.String(length=200)),
        nullable=False,
    )
    op.alter_column(
        "blacklist_entries",
        "profile_kind",
        existing_type=sa.String(length=20),
        server_default=None,
    )
    op.alter_column(
        "blacklist_entries",
        "subject_label",
        existing_type=sa.String(length=30),
        server_default=None,
    )
    op.alter_column(
        "blacklist_entries",
        "character_name",
        existing_type=sa.String(length=100),
        new_column_name="subject_name",
    )
    op.alter_column(
        "blacklist_entries",
        "character_name_key",
        existing_type=sa.String(length=100),
        new_column_name="subject_name_key",
    )
    op.drop_column("blacklist_entries", "pcu_profile_id")
    op.drop_column("blacklist_entries", "pcu_url")
    op.create_check_constraint(
        "ck_blacklist_profile_kind",
        "blacklist_entries",
        "profile_kind IN ('character','account')",
    )
    op.create_check_constraint(
        "ck_blacklist_profile_count",
        "blacklist_entries",
        "cardinality(pcu_profile_ids) BETWEEN 1 AND 5",
    )
    op.create_check_constraint(
        "ck_blacklist_profile_ids_positive",
        "blacklist_entries",
        "0 < ALL(pcu_profile_ids)",
    )
    op.create_check_constraint(
        "ck_blacklist_profile_cardinality",
        "blacklist_entries",
        "cardinality(pcu_urls) = cardinality(pcu_profile_ids)",
    )
    op.create_index(
        "ix_blacklist_profile_ids",
        "blacklist_entries",
        ["pcu_profile_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_blacklist_guild_subject",
        "blacklist_entries",
        ["guild_id", "subject_name_key", "status"],
    )
    _extend_configuration(op.get_bind())


def downgrade() -> None:
    _restore_configuration(op.get_bind())
    op.drop_index("ix_blacklist_guild_subject", table_name="blacklist_entries")
    op.drop_index("ix_blacklist_profile_ids", table_name="blacklist_entries")
    op.drop_constraint(
        "ck_blacklist_profile_cardinality",
        "blacklist_entries",
        type_="check",
    )
    op.drop_constraint(
        "ck_blacklist_profile_count",
        "blacklist_entries",
        type_="check",
    )
    op.drop_constraint(
        "ck_blacklist_profile_ids_positive",
        "blacklist_entries",
        type_="check",
    )
    op.drop_constraint(
        "ck_blacklist_profile_kind",
        "blacklist_entries",
        type_="check",
    )
    op.add_column(
        "blacklist_entries",
        sa.Column("pcu_profile_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "blacklist_entries",
        sa.Column("pcu_url", sa.String(length=200), nullable=True),
    )
    op.execute("UPDATE blacklist_entries SET pcu_profile_id = pcu_profile_ids[1], pcu_url = pcu_urls[1]")
    op.alter_column(
        "blacklist_entries",
        "pcu_profile_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "blacklist_entries",
        "pcu_url",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.alter_column(
        "blacklist_entries",
        "subject_name",
        existing_type=sa.String(length=100),
        new_column_name="character_name",
    )
    op.alter_column(
        "blacklist_entries",
        "subject_name_key",
        existing_type=sa.String(length=100),
        new_column_name="character_name_key",
    )
    op.drop_column("blacklist_entries", "subject_label")
    op.drop_column("blacklist_entries", "pcu_urls")
    op.drop_column("blacklist_entries", "pcu_profile_ids")
    op.drop_column("blacklist_entries", "profile_kind")
    op.create_check_constraint(
        "ck_blacklist_pcu_profile_positive",
        "blacklist_entries",
        "pcu_profile_id > 0",
    )
    op.create_index(
        "ix_blacklist_guild_pcu",
        "blacklist_entries",
        ["guild_id", "pcu_profile_id", "status"],
    )
    op.create_index(
        "ix_blacklist_guild_character",
        "blacklist_entries",
        ["guild_id", "character_name_key", "status"],
    )


def _extend_configuration(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT setting.id, setting.payload "
            "FROM guild_feature_settings setting "
            "JOIN guild_config_revisions revision "
            "ON revision.id = setting.config_revision_id "
            "WHERE revision.guild_id = :guild_id AND setting.feature_key = :feature_key"
        ),
        {"guild_id": LSSD_GUILD_ID, "feature_key": FEATURE_KEY},
    ).mappings()
    for row in rows:
        payload = _payload_dict(row["payload"])
        types = list(payload.get("types", []))
        for item in types:
            if item.get("key") == "ccw":
                item.update(
                    {
                        "profile_kind": "character",
                        "max_profiles": 1,
                        "subject_label": "Personaje",
                    }
                )
        faction_index = next(
            (index for index, item in enumerate(types) if item.get("key") == "faction"),
            None,
        )
        if faction_index is None:
            types.append(dict(FACTION_TYPE))
        else:
            types[faction_index] = {**types[faction_index], **FACTION_TYPE}
        payload["types"] = types
        _update_payload(connection, row["id"], payload)


def _restore_configuration(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT setting.id, setting.payload "
            "FROM guild_feature_settings setting "
            "JOIN guild_config_revisions revision "
            "ON revision.id = setting.config_revision_id "
            "WHERE revision.guild_id = :guild_id AND setting.feature_key = :feature_key"
        ),
        {"guild_id": LSSD_GUILD_ID, "feature_key": FEATURE_KEY},
    ).mappings()
    for row in rows:
        payload = _payload_dict(row["payload"])
        restored_types: list[dict[str, object]] = []
        for source in payload.get("types", []):
            item = dict(source)
            if item.get("key") == "faction":
                continue
            item.pop("profile_kind", None)
            item.pop("max_profiles", None)
            item.pop("subject_label", None)
            restored_types.append(item)
        payload["types"] = restored_types
        _update_payload(connection, row["id"], payload)


def _payload_dict(raw: object) -> dict[str, object]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("blacklists feature payload must be a JSON object")
    return dict(value)


def _update_payload(
    connection: sa.Connection,
    setting_id: object,
    payload: dict[str, object],
) -> None:
    connection.execute(
        sa.text(
            "UPDATE guild_feature_settings "
            "SET payload = CAST(:payload AS jsonb) "
            "WHERE id = :setting_id"
        ),
        {
            "setting_id": setting_id,
            "payload": json.dumps(payload, separators=(",", ":")),
        },
    )
