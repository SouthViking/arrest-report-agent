"""Validated, provider-backed bot personas loaded once at startup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

PERSONA_SCHEMA_VERSION = 1
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


class PersonaVoice(FrozenPersonaModel):
    """Compact traits rendered into the agent's identity instructions."""

    summary: str = Field(min_length=1, max_length=500)
    traits: tuple[str, ...] = Field(min_length=1, max_length=12)
    communication: tuple[str, ...] = Field(min_length=1, max_length=12)
    boundaries: tuple[str, ...] = Field(min_length=1, max_length=12)

    @field_validator("traits", "communication", "boundaries")
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


class BotPersona(FrozenPersonaModel):
    """Versioned persona contract independent from its storage provider."""

    schema_version: int = Field(ge=1)
    id: str = Field(pattern=PERSONA_ID_PATTERN.pattern)
    revision: int = Field(ge=1)
    discord: DiscordPersona
    identity: PersonaIdentity
    personality: PersonaVoice
    presence: PersonaPresence

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != PERSONA_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported persona schema version {value}; expected {PERSONA_SCHEMA_VERSION}"
            )
        return value


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
