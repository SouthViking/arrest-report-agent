from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from arrest_report_agent.config import AppSettings


def test_settings_parse_channel_ids_and_identity(settings: AppSettings) -> None:
    assert settings.discord_guild_ids == frozenset({1234674521416663070})
    assert settings.review_channel_ids == frozenset(
        {1462992794569281617, 1462992859761217745}
    )
    assert settings.identity.display_name == "Sgt. Vigil"
    assert settings.bot_idle_activity == "⭐ De guardia"
    assert settings.bot_busy_activity == "📂 Revisando expedientes"
    assert settings.authorized_user_ids is None
    assert settings.user_allowlist.allows(999) is True
    assert settings.openai_model == "gpt-5.6-terra"
    assert settings.openai_reasoning_effort == "medium"
    assert settings.log_json_indented is True


def test_empty_manual_role_is_none() -> None:
    settings = AppSettings(
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID="1",
        REVIEW_CHANNEL_IDS="2",
        MANUAL_REVIEW_ROLE_ID="",
    )
    assert settings.manual_review_role_id is None


def test_json_log_indentation_can_be_disabled() -> None:
    settings = AppSettings(
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID="1",
        REVIEW_CHANNEL_IDS="2",
        LOG_JSON_INDENTED="false",
    )

    assert settings.log_json_indented is False


@pytest.mark.parametrize(
    ("variable_name", "allowlist_attribute"),
    [
        ("DISCORD_GUILD_IDS", "guild_allowlist"),
        ("REVIEW_CHANNEL_IDS", "channel_allowlist"),
        ("AUTHORIZED_USER_IDS", "user_allowlist"),
    ],
)
@pytest.mark.parametrize("configured", [None, "", "*"])
def test_all_allowlists_default_to_unrestricted(
    variable_name: str,
    allowlist_attribute: str,
    configured: str | None,
) -> None:
    values = {
        "DISCORD_TOKEN": "discord",
        "OPENAI_API_KEY": "openai",
    }
    if configured is not None:
        values[variable_name] = configured

    settings = AppSettings(_env_file=None, **values)
    allowlist = getattr(settings, allowlist_attribute)

    assert allowlist.ids is None
    assert allowlist.is_restricted is False
    assert allowlist.configured_count == 0
    assert allowlist.allows(123) is True


@pytest.mark.parametrize(
    ("variable_name", "allowlist_attribute"),
    [
        ("DISCORD_GUILD_IDS", "guild_allowlist"),
        ("REVIEW_CHANNEL_IDS", "channel_allowlist"),
        ("AUTHORIZED_USER_IDS", "user_allowlist"),
    ],
)
def test_all_allowlists_parse_csv_and_restrict_access(
    variable_name: str,
    allowlist_attribute: str,
) -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        **{variable_name: "123, 456,123"},
    )
    allowlist = getattr(settings, allowlist_attribute)

    assert allowlist.ids == frozenset({123, 456})
    assert allowlist.is_restricted is True
    assert allowlist.configured_count == 2
    assert allowlist.allows(123) is True
    assert allowlist.allows(999) is False


@pytest.mark.parametrize(
    "variable_name",
    ["DISCORD_GUILD_IDS", "REVIEW_CHANNEL_IDS", "AUTHORIZED_USER_IDS"],
)
@pytest.mark.parametrize("configured", ["*,123", "0", "not-a-snowflake"])
def test_invalid_allowlist_configuration_is_rejected(
    variable_name: str,
    configured: str,
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            **{variable_name: configured},
        )


def test_legacy_singular_guild_variable_remains_supported() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID="123",
    )

    assert settings.discord_guild_ids == frozenset({123})


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            DISCORD_TOKEN="",
            OPENAI_API_KEY="openai",
            DISCORD_GUILD_ID="1",
            REVIEW_CHANNEL_IDS="2",
        )


def test_default_env_template_values_are_parseable(tmp_path) -> None:
    source = Path("default.env").read_text(encoding="utf-8")
    populated = source.replace("DISCORD_TOKEN=\n", "DISCORD_TOKEN=discord\n").replace(
        "OPENAI_API_KEY=\n", "OPENAI_API_KEY=openai\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(populated, encoding="utf-8")

    settings = AppSettings(_env_file=env_file)

    assert settings.thread_auto_archive_minutes == 1440
    assert settings.discord_guild_ids == frozenset({1234674521416663070})
    assert settings.authorized_user_ids is None
    assert settings.review_channel_ids == frozenset(
        {1462992794569281617, 1462992859761217745}
    )
