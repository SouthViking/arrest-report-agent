"""Validated environment configuration."""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property, lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotIdentity(BaseModel):
    """Runtime identity injected into prompts and Discord copy."""

    display_name: str
    role_title: str


class AppSettings(BaseSettings):
    """Process settings loaded from environment or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    discord_token: SecretStr = Field(alias="DISCORD_TOKEN")
    openai_api_key: SecretStr = Field(alias="OPENAI_API_KEY")
    discord_guild_id: int = Field(alias="DISCORD_GUILD_ID", gt=0)
    review_channel_ids_csv: str = Field(alias="REVIEW_CHANNEL_IDS")
    manual_review_role_id: int | None = Field(default=None, alias="MANUAL_REVIEW_ROLE_ID")

    bot_display_name: str = Field(default="Sgt. Vigil", alias="BOT_DISPLAY_NAME", min_length=1)
    bot_role_title: str = Field(default="Watch Sergeant", alias="BOT_ROLE_TITLE", min_length=1)

    app_environment: str = Field(default="local", alias="APP_ENV", min_length=1)
    app_version: str = Field(default="0.1.0", alias="APP_VERSION", min_length=1)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json_indented: bool = Field(default=True, alias="LOG_JSON_INDENTED")
    health_host: str = Field(default="0.0.0.0", alias="HEALTH_HOST")
    health_port: int = Field(default=8080, alias="HEALTH_PORT", ge=1, le=65535)

    openai_model: str = Field(default="gpt-5.6-terra", alias="OPENAI_MODEL", min_length=1)
    openai_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = Field(
        default="medium", alias="OPENAI_REASONING_EFFORT"
    )
    openai_image_detail: Literal["low", "high", "auto", "original"] = Field(
        default="original", alias="OPENAI_IMAGE_DETAIL"
    )
    openai_text_verbosity: Literal["low", "medium", "high"] = Field(
        default="low", alias="OPENAI_TEXT_VERBOSITY"
    )
    openai_max_output_tokens: int = Field(default=2500, alias="OPENAI_MAX_OUTPUT_TOKENS", ge=256)
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES", ge=0, le=10)
    openai_tracing_enabled: bool = Field(default=True, alias="OPENAI_TRACING_ENABLED")
    openai_trace_include_sensitive_data: bool = Field(
        default=False, alias="OPENAI_TRACE_INCLUDE_SENSITIVE_DATA"
    )
    trace_workflow_name: str = Field(
        default="arrest_report_review", alias="TRACE_WORKFLOW_NAME", min_length=1
    )

    openai_input_cost_per_million: Decimal = Field(
        default=Decimal("2.50"), alias="OPENAI_INPUT_COST_PER_MILLION", ge=0
    )
    openai_cached_input_cost_per_million: Decimal = Field(
        default=Decimal("0.25"), alias="OPENAI_CACHED_INPUT_COST_PER_MILLION", ge=0
    )
    openai_output_cost_per_million: Decimal = Field(
        default=Decimal("15.00"), alias="OPENAI_OUTPUT_COST_PER_MILLION", ge=0
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
    def validate_secrets_and_channels(self) -> AppSettings:
        if not self.discord_token.get_secret_value().strip():
            raise ValueError("DISCORD_TOKEN must not be empty")
        if not self.openai_api_key.get_secret_value().strip():
            raise ValueError("OPENAI_API_KEY must not be empty")
        if not self.review_channel_ids:
            raise ValueError("REVIEW_CHANNEL_IDS must contain at least one channel ID")
        return self

    @cached_property
    def review_channel_ids(self) -> frozenset[int]:
        values: set[int] = set()
        for raw_value in self.review_channel_ids_csv.split(","):
            normalized = raw_value.strip()
            if not normalized:
                continue
            channel_id = int(normalized)
            if channel_id <= 0:
                raise ValueError("REVIEW_CHANNEL_IDS must contain positive Discord snowflakes")
            values.add(channel_id)
        return frozenset(values)

    @property
    def identity(self) -> BotIdentity:
        return BotIdentity(display_name=self.bot_display_name, role_title=self.bot_role_title)


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    """Load and cache process settings."""

    return AppSettings()  # type: ignore[call-arg]
