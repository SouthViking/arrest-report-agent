from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from arrest_report_agent.config import AppSettings


def test_settings_parse_channel_ids_and_persona_selection(settings: AppSettings) -> None:
    assert settings.discord_guild_ids == frozenset({1234674521416663070})
    assert settings.report_channel_ids == frozenset({1462992794569281617, 1462992859761217745})
    assert settings.bot_persona == "walter_vigil"
    assert settings.authorized_user_ids is None
    assert settings.user_allowlist.allows(999) is True
    assert settings.supervisor_role_ids is None
    assert settings.supervisor_role_allowlist.allows(999) is True
    assert settings.role_request_allowed_channel_ids is None
    assert settings.role_request_reviewer_role_ids == ()
    assert settings.role_request_allowed_role_ids == ()
    assert settings.role_request_allowed_role_allowlist.ids is None
    assert settings.openai_model == "gpt-5.6-terra"
    assert settings.openai_reasoning_effort == "medium"
    assert settings.log_json_indented is True
    assert settings.max_files_per_review == 5
    assert settings.max_file_bytes == 10_485_760
    assert settings.max_total_file_bytes == 52_428_800
    assert settings.channel_context_max_messages == 5
    assert settings.thread_context_max_historical_files == 2
    assert settings.app_version == "0.18.1"


def test_empty_escalation_roles_disable_mentions_and_legacy_name_is_supported() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID="1",
        REPORT_CHANNEL_IDS="2",
        MANUAL_REVIEW_ROLE_ID="",
    )
    assert settings.review_escalation_role_ids == ()

    legacy = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        MANUAL_REVIEW_ROLE_ID="123",
    )
    assert legacy.review_escalation_role_ids == (123,)


def test_escalation_roles_parse_csv_in_order_and_remove_duplicates() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        REVIEW_ESCALATION_ROLE_IDS="123,456,123",
    )

    assert settings.review_escalation_role_ids == (123, 456)


@pytest.mark.parametrize("configured", ["*", "123,*", "invalid", "0", "-1"])
def test_escalation_roles_reject_open_or_invalid_values(configured: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            REVIEW_ESCALATION_ROLE_IDS=configured,
        )


def test_json_log_indentation_can_be_disabled() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        DISCORD_GUILD_ID="1",
        REPORT_CHANNEL_IDS="2",
        LOG_JSON_INDENTED="false",
    )

    assert settings.log_json_indented is False


def test_railway_port_takes_precedence_over_local_health_port() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        PORT="9090",
        HEALTH_PORT="8080",
    )

    assert settings.health_port == 9090


def test_local_health_port_remains_supported() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        HEALTH_PORT="8181",
    )

    assert settings.health_port == 8181


def test_railway_webhook_requires_complete_identity_and_url_safe_secret() -> None:
    values = {
        "_env_file": None,
        "DISCORD_TOKEN": "discord",
        "OPENAI_API_KEY": "openai",
        "RAILWAY_WEBHOOK_SECRET": "a" * 43,
        "RAILWAY_PROJECT_ID": "project",
        "RAILWAY_ENVIRONMENT_ID": "production",
        "RAILWAY_SERVICE_ID": "bot",
    }

    settings = AppSettings(**values)

    assert settings.railway_webhook_enabled is True
    assert settings.railway_webhook_identity_is_complete is True
    assert settings.app_version == "0.18.1"

    with pytest.raises(ValidationError, match="RAILWAY_ENVIRONMENT_ID"):
        AppSettings(**{key: value for key, value in values.items() if key != "RAILWAY_ENVIRONMENT_ID"})
    with pytest.raises(ValidationError, match="URL-safe"):
        AppSettings(**{**values, "RAILWAY_WEBHOOK_SECRET": "/" * 43})


def test_github_webhook_requires_a_strong_shared_secret() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        GITHUB_WEBHOOK_SECRET="g" * 32,
    )

    assert settings.github_webhook_enabled is True

    with pytest.raises(ValidationError, match="32-256"):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            GITHUB_WEBHOOK_SECRET="too-short",
        )


def test_cost_monitoring_credentials_are_optional_and_independent() -> None:
    openai = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        OPENAI_ADMIN_KEY="admin",
        OPENAI_PROJECT_ID="proj_test",
    )
    railway = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        RAILWAY_API_TOKEN="workspace-token",
        RAILWAY_WORKSPACE_ID="workspace-id",
    )

    assert openai.openai_cost_monitoring_enabled is True
    assert openai.railway_cost_monitoring_enabled is False
    assert railway.openai_cost_monitoring_enabled is False
    assert railway.railway_cost_monitoring_enabled is True


def test_sarp_client_settings_are_optional_normalized_and_secret() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        SARP_API_ENABLED="true",
        SARP_API_TOKEN="  sarp_secret  ",
        SARP_API_BASE_URL="https://api.gta-rol.com/api/",
        SARP_API_TIMEOUT_SECONDS="12",
        SARP_API_MAX_RETRIES="3",
        SARP_EMERGENCY_CALL_POLL_SECONDS="45",
        SARP_STOLEN_VEHICLE_POLL_SECONDS="75",
    )

    assert settings.sarp_api_token is not None
    assert settings.sarp_api_enabled is True
    assert settings.sarp_api_token.get_secret_value() == "sarp_secret"
    assert "sarp_secret" not in repr(settings)
    assert settings.sarp_api_base_url == "https://api.gta-rol.com/api"
    assert settings.sarp_api_timeout_seconds == 12
    assert settings.sarp_api_max_retries == 3
    assert settings.sarp_emergency_call_poll_seconds == 45
    assert settings.sarp_stolen_vehicle_poll_seconds == 75


def test_sarp_api_is_disabled_by_default_and_enabled_mode_requires_token() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
    )
    assert settings.sarp_api_enabled is False

    with pytest.raises(ValidationError, match="SARP_API_TOKEN"):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            SARP_API_ENABLED="true",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "api.gta-rol.com/api",
        "http://api.gta-rol.com/api",
        "https://user:password@api.gta-rol.com/api",
        "https://api.gta-rol.com/api?token=secret",
    ],
)
def test_sarp_client_settings_reject_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValidationError, match="SARP_API_BASE_URL"):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            SARP_API_BASE_URL=base_url,
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("OPENAI_PROJECT_ID", "proj_test", "OPENAI_ADMIN_KEY"),
        ("RAILWAY_API_TOKEN", "token", "RAILWAY_WORKSPACE_ID"),
        ("RAILWAY_WORKSPACE_ID", "workspace", "RAILWAY_API_TOKEN"),
    ],
)
def test_cost_monitoring_rejects_incomplete_credentials(
    key: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            **{key: value},
        )


@pytest.mark.parametrize(
    ("variable_name", "allowlist_attribute"),
    [
        ("DISCORD_GUILD_IDS", "guild_allowlist"),
        ("REPORT_CHANNEL_IDS", "report_channel_allowlist"),
        ("AUTHORIZED_USER_IDS", "user_allowlist"),
        ("SUPERVISOR_ROLE_IDS", "supervisor_role_allowlist"),
        ("ROLE_REQUEST_ALLOWED_CHANNEL_IDS", "role_request_allowed_channel_allowlist"),
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
        ("REPORT_CHANNEL_IDS", "report_channel_allowlist"),
        ("AUTHORIZED_USER_IDS", "user_allowlist"),
        ("SUPERVISOR_ROLE_IDS", "supervisor_role_allowlist"),
        ("ROLE_REQUEST_ALLOWED_CHANNEL_IDS", "role_request_allowed_channel_allowlist"),
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
    [
        "DISCORD_GUILD_IDS",
        "REPORT_CHANNEL_IDS",
        "AUTHORIZED_USER_IDS",
        "SUPERVISOR_ROLE_IDS",
        "ROLE_REQUEST_ALLOWED_CHANNEL_IDS",
    ],
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


def test_role_request_reviewer_roles_enable_subsystem_with_explicit_ids() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        ROLE_REQUEST_ALLOWED_CHANNEL_IDS="123,456",
        ROLE_REQUEST_REVIEWER_ROLE_IDS="700, 800,700",
        ROLE_REQUEST_ALLOWED_ROLE_IDS="900, 901,900",
    )

    assert settings.role_request_allowed_channel_ids == frozenset({123, 456})
    assert settings.role_request_reviewer_role_ids == (700, 800)
    assert settings.role_request_allowed_role_ids == (900, 901)


@pytest.mark.parametrize("configured", ["*", "123,*", "invalid", "0", "-1"])
def test_role_request_reviewer_roles_reject_open_or_invalid_values(
    configured: str,
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            ROLE_REQUEST_REVIEWER_ROLE_IDS=configured,
        )


@pytest.mark.parametrize("configured", ["", "*"])
def test_role_request_allowed_roles_support_open_values(configured: str) -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        ROLE_REQUEST_ALLOWED_ROLE_IDS=configured,
    )

    assert settings.role_request_allowed_role_allowlist.ids is None
    assert settings.role_request_allowed_role_ids == ()


@pytest.mark.parametrize("configured", ["123,*", "invalid", "0", "-1"])
def test_role_request_allowed_roles_reject_mixed_or_invalid_values(
    configured: str,
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            ROLE_REQUEST_ALLOWED_ROLE_IDS=configured,
        )


def test_role_request_reviewer_roles_allow_an_open_requestable_catalog() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        ROLE_REQUEST_REVIEWER_ROLE_IDS="700",
    )

    assert settings.role_request_allowed_role_allowlist.ids is None


def test_role_request_catalog_cannot_include_reviewer_roles() -> None:
    with pytest.raises(ValidationError, match="cannot include reviewer roles"):
        AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            ROLE_REQUEST_REVIEWER_ROLE_IDS="700",
            ROLE_REQUEST_ALLOWED_ROLE_IDS="701,700",
        )


def test_legacy_review_channel_variable_remains_supported() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        REVIEW_CHANNEL_IDS="123,456",
    )

    assert settings.report_channel_ids == frozenset({123, 456})


def test_legacy_allowed_channel_variable_remains_supported() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        ALLOWED_CHANNEL_IDS="123,456",
    )

    assert settings.report_channel_ids == frozenset({123, 456})


def test_report_channel_variable_takes_precedence_over_legacy_aliases() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        REPORT_CHANNEL_IDS="123",
        ALLOWED_CHANNEL_IDS="456",
        REVIEW_CHANNEL_IDS="789",
    )

    assert settings.report_channel_ids == frozenset({123})


def test_missing_legacy_persona_is_allowed_for_database_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOT_PERSONA", raising=False)
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
    )
    assert settings.bot_persona is None


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            DISCORD_TOKEN="",
            OPENAI_API_KEY="openai",
            DISCORD_GUILD_ID="1",
            REPORT_CHANNEL_IDS="2",
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
    assert settings.bot_persona == "walter_vigil"
    assert settings.discord_guild_ids == frozenset({1234674521416663070})
    assert settings.authorized_user_ids is None
    assert settings.supervisor_role_ids is None
    assert settings.report_channel_ids == frozenset({1462992794569281617, 1462992859761217745})
    assert settings.role_request_allowed_channel_ids is None
    assert settings.role_request_reviewer_role_ids == ()
    assert settings.role_request_allowed_role_ids == ()
    assert settings.role_request_allowed_role_allowlist.ids is None
