"""Validated environment configuration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotIdentity(BaseModel):
    """Runtime identity injected into prompts and Discord copy."""

    display_name: str
    role_title: str


@dataclass(frozen=True, slots=True)
class SnowflakeAllowlist:
    """Uniform open-or-restricted policy for Discord snowflake IDs."""

    ids: frozenset[int] | None

    @classmethod
    def parse(cls, raw_value: str, *, variable_name: str) -> SnowflakeAllowlist:
        normalized = raw_value.strip()
        if not normalized or normalized == "*":
            return cls(ids=None)

        raw_ids = [value.strip() for value in normalized.split(",") if value.strip()]
        if "*" in raw_ids:
            raise ValueError(f"{variable_name} cannot combine '*' with Discord IDs")

        ids: set[int] = set()
        for raw_id in raw_ids:
            try:
                snowflake = int(raw_id)
            except ValueError as error:
                raise ValueError(
                    f"{variable_name} must contain positive Discord snowflakes"
                ) from error
            if snowflake <= 0:
                raise ValueError(f"{variable_name} must contain positive Discord snowflakes")
            ids.add(snowflake)
        return cls(ids=frozenset(ids) or None)

    @property
    def is_restricted(self) -> bool:
        return self.ids is not None

    @property
    def configured_count(self) -> int:
        return len(self.ids or ())

    def allows(self, snowflake: int) -> bool:
        return self.ids is None or snowflake in self.ids


class AppSettings(BaseSettings):
    """Process settings loaded from environment or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    discord_token: SecretStr = Field(alias="DISCORD_TOKEN")
    openai_api_key: SecretStr = Field(alias="OPENAI_API_KEY")
    discord_guild_ids_csv: str = Field(
        default="*",
        validation_alias=AliasChoices("DISCORD_GUILD_IDS", "DISCORD_GUILD_ID"),
    )
    review_channel_ids_csv: str = Field(default="*", alias="REVIEW_CHANNEL_IDS")
    authorized_user_ids_csv: str = Field(default="*", alias="AUTHORIZED_USER_IDS")
    manual_review_role_id: int | None = Field(default=None, alias="MANUAL_REVIEW_ROLE_ID")

    bot_display_name: str = Field(default="Sgt. Vigil", alias="BOT_DISPLAY_NAME", min_length=1)
    bot_role_title: str = Field(default="Watch Sergeant", alias="BOT_ROLE_TITLE", min_length=1)
    bot_idle_activity: str = Field(
        default="⭐ De guardia",
        alias="BOT_IDLE_ACTIVITY",
        min_length=1,
        max_length=128,
    )
    bot_busy_activity: str = Field(
        default="📂 Revisando expedientes",
        alias="BOT_BUSY_ACTIVITY",
        min_length=1,
        max_length=128,
    )

    app_environment: str = Field(default="local", alias="APP_ENV", min_length=1)
    app_version: str = Field(default="0.1.0", alias="APP_VERSION", min_length=1)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json_indented: bool = Field(default=True, alias="LOG_JSON_INDENTED")
    health_host: str = Field(default="0.0.0.0", alias="HEALTH_HOST")
    health_port: int = Field(default=8080, alias="HEALTH_PORT", ge=1, le=65535)

    openai_model: str = Field(default="gpt-5.6-terra", alias="OPENAI_MODEL", min_length=1)
    openai_reasoning_effort: str = Field(
        default="medium", alias="OPENAI_REASONING_EFFORT", min_length=1
    )
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES", ge=0, le=10)
    openai_tracing_enabled: bool = Field(default=True, alias="OPENAI_TRACING_ENABLED")
    openai_trace_include_sensitive_data: bool = Field(
        default=False, alias="OPENAI_TRACE_INCLUDE_SENSITIVE_DATA"
    )
    trace_workflow_name: str = Field(
        default="arrest_report_review", alias="TRACE_WORKFLOW_NAME", min_length=1
    )

    review_timeout_seconds: float = Field(default=180, alias="REVIEW_TIMEOUT_SECONDS", gt=0)
    max_agent_turns: int = Field(default=12, alias="MAX_AGENT_TURNS", ge=2, le=50)
    max_concurrent_reviews: int = Field(default=2, alias="MAX_CONCURRENT_REVIEWS", ge=1, le=20)
    max_images_per_review: int = Field(default=10, alias="MAX_IMAGES_PER_REVIEW", ge=1, le=30)
    max_image_bytes: int = Field(default=10_485_760, alias="MAX_IMAGE_BYTES", ge=1)
    thread_context_max_messages: int = Field(
        default=20, alias="THREAD_CONTEXT_MAX_MESSAGES", ge=1, le=100
    )
    thread_context_max_chars: int = Field(default=8000, alias="THREAD_CONTEXT_MAX_CHARS", ge=500)
    thread_context_max_message_chars: int = Field(
        default=1500, alias="THREAD_CONTEXT_MAX_MESSAGE_CHARS", ge=100
    )
    thread_context_max_historical_images: int = Field(
        default=2, alias="THREAD_CONTEXT_MAX_HISTORICAL_IMAGES", ge=0, le=10
    )
    thread_auto_archive_minutes: int = Field(
        default=1440, alias="THREAD_AUTO_ARCHIVE_MINUTES"
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("manual_review_role_id", mode="before")
    @classmethod
    def parse_optional_snowflake(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("thread_auto_archive_minutes")
    @classmethod
    def validate_archive_duration(cls, value: int) -> int:
        allowed = {60, 1440, 4320, 10080}
        if value not in allowed:
            raise ValueError(f"must be one of {sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def validate_secrets_and_allowlists(self) -> AppSettings:
        if not self.discord_token.get_secret_value().strip():
            raise ValueError("DISCORD_TOKEN must not be empty")
        if not self.openai_api_key.get_secret_value().strip():
            raise ValueError("OPENAI_API_KEY must not be empty")
        _ = self.guild_allowlist, self.channel_allowlist, self.user_allowlist
        return self

    @property
    def guild_allowlist(self) -> SnowflakeAllowlist:
        return SnowflakeAllowlist.parse(
            self.discord_guild_ids_csv,
            variable_name="DISCORD_GUILD_IDS",
        )

    @property
    def channel_allowlist(self) -> SnowflakeAllowlist:
        return SnowflakeAllowlist.parse(
            self.review_channel_ids_csv,
            variable_name="REVIEW_CHANNEL_IDS",
        )

    @property
    def user_allowlist(self) -> SnowflakeAllowlist:
        return SnowflakeAllowlist.parse(
            self.authorized_user_ids_csv,
            variable_name="AUTHORIZED_USER_IDS",
        )

    @property
    def discord_guild_ids(self) -> frozenset[int] | None:
        return self.guild_allowlist.ids

    @property
    def review_channel_ids(self) -> frozenset[int] | None:
        return self.channel_allowlist.ids

    @property
    def authorized_user_ids(self) -> frozenset[int] | None:
        return self.user_allowlist.ids

    @property
    def identity(self) -> BotIdentity:
        return BotIdentity(display_name=self.bot_display_name, role_title=self.bot_role_title)


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    """Load and cache process settings."""

    return AppSettings()  # type: ignore[call-arg]
