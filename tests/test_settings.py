from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from arrest_report_agent.config import AppSettings


def test_settings_parse_channel_ids_and_identity(settings: AppSettings) -> None:
    assert settings.review_channel_ids == frozenset(
        {1462992794569281617, 1462992859761217745}
    )
    assert settings.identity.display_name == "Sgt. Vigil"
    assert settings.openai_model == "gpt-5.6-terra"
    assert settings.openai_reasoning_effort == "medium"
    assert settings.log_json_indented is True


def test_empty_manual_role_is_none() -> None:
    settings = AppSettings(
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID=1,
        REVIEW_CHANNEL_IDS="2",
        MANUAL_REVIEW_ROLE_ID="",
    )
    assert settings.manual_review_role_id is None


def test_json_log_indentation_can_be_disabled() -> None:
    settings = AppSettings(
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID=1,
        REVIEW_CHANNEL_IDS="2",
        LOG_JSON_INDENTED="false",
    )

    assert settings.log_json_indented is False


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            DISCORD_TOKEN="",
            OPENAI_API_KEY="openai",
            DISCORD_GUILD_ID=1,
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
    assert settings.review_channel_ids == frozenset(
        {1462992794569281617, 1462992859761217745}
    )
