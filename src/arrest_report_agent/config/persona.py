"""Validated, provider-backed bot personas loaded once at startup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from string import Formatter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PERSONA_SCHEMA_VERSION = 4
PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
MAX_PROFILE_ASSET_BYTES = 10_485_760
SUPPORTED_PROFILE_ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})


class PersonaConfigurationError(RuntimeError):
    """Raised when a selected persona or its avatar is invalid."""


class FrozenPersonaModel(BaseModel):
    """Common strict configuration for immutable persona sections."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DiscordPersona(FrozenPersonaModel):
    """Global Discord profile fields managed by the bot."""

    username: str = Field(min_length=2, max_length=32)
    nickname: str | None = Field(default=None, min_length=1, max_length=32)
    bio: str = Field(min_length=1, max_length=190)
    avatar: str = Field(min_length=1)


class PersonaIdentity(FrozenPersonaModel):
    """In-character identity used by prompts and deterministic Discord copy."""

    display_name: str = Field(min_length=1, max_length=100)
    full_name: str = Field(min_length=1, max_length=100)
    rank: str = Field(min_length=1, max_length=100)
    role_title: str = Field(min_length=1, max_length=100)
    organization_name: str = Field(min_length=1, max_length=200)
    organization_acronym: str = Field(min_length=1, max_length=30)
    default_form_of_address: str = Field(min_length=1, max_length=50)
    language: str = Field(min_length=2, max_length=16)
    setting: str = Field(min_length=1, max_length=300)


class PersonaCommunication(FrozenPersonaModel):
    """Persona voice split between natural conversation and review summaries."""

    conversation: tuple[str, ...] = Field(min_length=1, max_length=12)
    review_summary: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("conversation", "review_summary")
    @classmethod
    def reject_empty_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona communication lists cannot contain empty items")
        return values


class PersonaConversationExample(FrozenPersonaModel):
    """One compact natural-conversation example for the selected voice."""

    situation: str = Field(min_length=1, max_length=100)
    user: str = Field(min_length=1, max_length=300)
    assistant: str = Field(min_length=1, max_length=500)


class PersonaReviewSummaryExample(FrozenPersonaModel):
    """One example limited to the personalized opening of a review result."""

    verdict: Literal["approved", "corrections_required", "inconclusive"]
    situation: str = Field(min_length=1, max_length=150)
    summary: str = Field(min_length=1, max_length=500)


class PersonaExamples(FrozenPersonaModel):
    """Bounded few-shot examples kept separate by interaction context."""

    conversation: tuple[PersonaConversationExample, ...] = Field(
        min_length=1,
        max_length=6,
    )
    review_summary: tuple[PersonaReviewSummaryExample, ...] = Field(
        min_length=1,
        max_length=4,
    )


class PersonaVoice(FrozenPersonaModel):
    """Compact traits and context-specific voices rendered into the agent prompt."""

    summary: str = Field(min_length=1, max_length=500)
    traits: tuple[str, ...] = Field(min_length=1, max_length=12)
    communication: PersonaCommunication
    examples: PersonaExamples
    boundaries: tuple[str, ...] = Field(min_length=1, max_length=12)

    @field_validator("traits", "boundaries")
    @classmethod
    def reject_empty_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona lists cannot contain empty items")
        return values


class PersonaPresence(FrozenPersonaModel):
    """Discord presence copy for each supported activity mode."""

    idle: str = Field(min_length=1, max_length=128)
    conversation: str = Field(min_length=1, max_length=128)
    knowledge: str = Field(min_length=1, max_length=128)
    review: str = Field(min_length=1, max_length=128)


class PersonaAccessMessages(FrozenPersonaModel):
    """Deterministic access and command notices written in the persona's voice."""

    wrong_channel_intros: tuple[str, ...] = Field(min_length=1, max_length=8)
    wrong_channel_heading: str = Field(min_length=1, max_length=300)
    wrong_channel_destinations: str = Field(min_length=1, max_length=300)
    wrong_channel_hidden: str = Field(min_length=1, max_length=300)
    wrong_channel_uncertain: str = Field(min_length=1, max_length=300)
    wrong_channel_unavailable: str = Field(min_length=1, max_length=300)
    unauthorized_user_intros: tuple[str, ...] = Field(min_length=1, max_length=8)
    unauthorized_user: str = Field(min_length=1, max_length=400)
    scope_refusal: str = Field(min_length=1, max_length=400)
    supervisor_unauthorized: str = Field(min_length=1, max_length=400)
    other_member_query: str = Field(min_length=1, max_length=250)
    other_supervisor_query: str = Field(min_length=1, max_length=250)
    personal_wrong_guild: str = Field(min_length=1, max_length=300)
    personal_member_unavailable: str = Field(min_length=1, max_length=300)
    supervisor_wrong_guild: str = Field(min_length=1, max_length=300)
    supervisor_member_unavailable: str = Field(min_length=1, max_length=300)
    no_supervisor_channels: str = Field(min_length=1, max_length=300)
    no_personal_channels: str = Field(min_length=1, max_length=300)

    @field_validator("wrong_channel_intros", "unauthorized_user_intros")
    @classmethod
    def reject_empty_pool_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona message pools cannot contain empty items")
        return values

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaAccessMessages:
        _require_template_fields(
            self.wrong_channel_heading,
            {"form_of_address", "intro"},
            "wrong_channel_heading",
        )
        _require_template_fields(
            self.wrong_channel_destinations,
            {"channels"},
            "wrong_channel_destinations",
        )
        _require_template_fields(
            self.unauthorized_user,
            {"form_of_address", "intro"},
            "unauthorized_user",
        )
        _require_template_fields(
            self.scope_refusal,
            {"form_of_address"},
            "scope_refusal",
        )
        return self


class PersonaFailureMessages(FrozenPersonaModel):
    """Safe deterministic operational failures without provider details."""

    quota_exceeded: str = Field(min_length=1, max_length=500)
    rate_limited: str = Field(min_length=1, max_length=500)
    timeout: str = Field(min_length=1, max_length=500)
    provider_internal: str = Field(min_length=1, max_length=500)
    auth_configuration: str = Field(min_length=1, max_length=500)
    agent_execution: str = Field(min_length=1, max_length=500)
    discord_failure: str = Field(min_length=1, max_length=500)
    context_unavailable: str = Field(min_length=1, max_length=500)
    unknown: str = Field(min_length=1, max_length=500)
    review_failure_title: str = Field(min_length=1, max_length=256)
    review_failure_next_step: str = Field(min_length=1, max_length=500)
    charge_query_failure: str = Field(min_length=1, max_length=500)
    personal_report_query_failure: str = Field(min_length=1, max_length=500)
    personal_summary_failure: str = Field(min_length=1, max_length=500)
    supervision_query_failure: str = Field(min_length=1, max_length=500)


class PersonaHelpMessages(FrozenPersonaModel):
    """Personality-bearing copy surrounding the functional help fields."""

    description: str = Field(min_length=1, max_length=700)
    review_scope: str = Field(min_length=1, max_length=900)
    request_review: str = Field(min_length=1, max_length=900)
    thread_behavior: str = Field(min_length=1, max_length=900)
    footer: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaHelpMessages:
        _require_template_fields(
            self.description,
            {"display_name", "role_title"},
            "help.description",
        )
        _require_template_fields(
            self.request_review,
            {"bot_mention"},
            "help.request_review",
        )
        _require_template_fields(
            self.footer,
            {"form_of_address"},
            "help.footer",
        )
        return self


class PersonaMessages(FrozenPersonaModel):
    """Complete deterministic copy selected with the runtime persona."""

    access: PersonaAccessMessages
    failures: PersonaFailureMessages
    help: PersonaHelpMessages


class BotPersona(FrozenPersonaModel):
    """Versioned persona contract independent from its storage provider."""

    schema_version: int = Field(ge=1)
    id: str = Field(pattern=PERSONA_ID_PATTERN.pattern)
    revision: int = Field(ge=1)
    discord: DiscordPersona
    identity: PersonaIdentity
    personality: PersonaVoice
    presence: PersonaPresence
    messages: PersonaMessages

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != PERSONA_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported persona schema version {value}; expected {PERSONA_SCHEMA_VERSION}"
            )
        return value


def _require_template_fields(template: str, expected: set[str], label: str) -> None:
    fields: set[str] = set()
    try:
        parts = Formatter().parse(template)
        for _, field_name, format_spec, conversion in parts:
            if field_name is None:
                continue
            if not field_name or format_spec or conversion or any(
                token in field_name for token in (".", "[", "]")
            ):
                raise ValueError(f"persona message template '{label}' uses unsafe formatting")
            fields.add(field_name)
    except ValueError as error:
        raise ValueError(f"invalid persona message template '{label}': {error}") from error
    if fields != expected:
        raise ValueError(
            f"persona message template '{label}' must use placeholders {sorted(expected)}"
        )


@dataclass(frozen=True, slots=True)
class LoadedPersona:
    """A validated persona and its already-loaded avatar."""

    profile: BotPersona
    avatar_bytes: bytes


class PersonaProvider(Protocol):
    """Storage-independent provider that can later be backed by a database."""

    async def load(self, persona_id: str) -> LoadedPersona:
        """Load and validate one complete persona by stable identifier."""


class PackagedPersonaProvider:
    """Load personas and their avatars from package resources."""

    def __init__(self, root: Traversable | None = None) -> None:
        self._root = root or files("arrest_report_agent").joinpath("config", "personas")

    async def load(self, persona_id: str) -> LoadedPersona:
        normalized_id = persona_id.strip()
        if not PERSONA_ID_PATTERN.fullmatch(normalized_id):
            raise PersonaConfigurationError(
                "BOT_PERSONA must use lowercase letters, numbers and single underscores"
            )

        resource = self._root.joinpath(f"{normalized_id}.json")
        try:
            raw_profile = json.loads(resource.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise PersonaConfigurationError(
                f"unknown BOT_PERSONA '{normalized_id}'"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PersonaConfigurationError(
                f"unable to read persona '{normalized_id}': {error}"
            ) from error

        try:
            profile = BotPersona.model_validate(raw_profile)
        except Exception as error:
            raise PersonaConfigurationError(
                f"invalid persona '{normalized_id}': {error}"
            ) from error
        if profile.id != normalized_id:
            raise PersonaConfigurationError(
                f"persona id '{profile.id}' does not match BOT_PERSONA '{normalized_id}'"
            )

        avatar_bytes = self._load_asset(profile.discord.avatar, label="avatar")
        return LoadedPersona(
            profile=profile,
            avatar_bytes=avatar_bytes,
        )

    def _load_asset(self, reference: str, *, label: str) -> bytes:
        path = PurePosixPath(reference)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise PersonaConfigurationError(f"persona {label} path must remain inside its profile")
        if path.suffix.lower() not in SUPPORTED_PROFILE_ASSET_SUFFIXES:
            raise PersonaConfigurationError(
                f"persona {label} must be a PNG, JPEG or GIF image"
            )

        resource = self._root.joinpath(*path.parts)
        try:
            payload = resource.read_bytes()
        except (FileNotFoundError, OSError) as error:
            raise PersonaConfigurationError(
                f"persona {label} asset '{reference}' is unavailable"
            ) from error
        if not payload:
            raise PersonaConfigurationError(f"persona {label} asset cannot be empty")
        if len(payload) > MAX_PROFILE_ASSET_BYTES:
            raise PersonaConfigurationError(
                f"persona {label} asset exceeds {MAX_PROFILE_ASSET_BYTES} bytes"
            )
        if not _matches_image_signature(payload, path.suffix.lower()):
            raise PersonaConfigurationError(
                f"persona {label} asset '{reference}' does not match its image format"
            )
        return payload


def _matches_image_signature(payload: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return payload.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    return False
