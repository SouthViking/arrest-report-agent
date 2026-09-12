"""Domain models and normalization for structured faction blacklists."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

PCU_PROFILE_HOST = "ucp.sarp.es"
PCU_PROFILE_PATH = re.compile(r"^/app/ucp/characters/overview/([1-9][0-9]*)/?$")
BLACKLIST_REFERENCE = re.compile(r"^([A-Z][A-Z0-9-]{1,11})-([0-9]{1,19})$")


class BlacklistEntryStatus(StrEnum):
    PUBLISHING = "publishing"
    ACTIVE = "active"
    FAILED = "failed"
    REVOKED = "revoked"


class BlacklistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    type_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    type_name: str = Field(min_length=1, max_length=50)
    reference_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9-]{1,11}$")
    number: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    message_id: int | None = Field(default=None, gt=0)
    interaction_id: int = Field(gt=0)
    author_user_id: int = Field(gt=0)
    pcu_profile_id: int = Field(gt=0)
    pcu_url: str = Field(min_length=1, max_length=200)
    character_name: str = Field(min_length=3, max_length=100)
    character_name_key: str = Field(min_length=3, max_length=100)
    reason: str = Field(min_length=10, max_length=1_000)
    status: BlacklistEntryStatus
    edited_by_user_id: int | None = Field(default=None, gt=0)
    edited_at: datetime | None = None
    revoked_by_user_id: int | None = Field(default=None, gt=0)
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_audit_fields(self) -> BlacklistEntry:
        if (self.edited_by_user_id is None) != (self.edited_at is None):
            raise ValueError("blacklist edit audit fields must be set together")
        if (self.revoked_by_user_id is None) != (self.revoked_at is None):
            raise ValueError("blacklist revocation audit fields must be set together")
        if self.status is BlacklistEntryStatus.REVOKED and self.revoked_at is None:
            raise ValueError("revoked blacklist entries require revocation audit fields")
        if self.status is not BlacklistEntryStatus.REVOKED and self.revoked_at is not None:
            raise ValueError("active blacklist entries cannot have revocation audit fields")
        return self

    @property
    def reference(self) -> str:
        return f"{self.reference_prefix}-{self.number:05d}"

    @property
    def message_url(self) -> str | None:
        if self.message_id is None:
            return None
        return (
            f"https://discord.com/channels/{self.guild_id}/"
            f"{self.channel_id}/{self.message_id}"
        )


def parse_pcu_profile_url(raw_url: str) -> tuple[int, str]:
    """Validate one exact SARP PCU character URL and return its ID and canonical form."""

    value = raw_url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("La URL de PCU no es válida.") from error
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != PCU_PROFILE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Usa una URL de perfil válida de ucp.sarp.es.")
    match = PCU_PROFILE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("La URL debe terminar en /characters/overview/<id>.")
    profile_id = int(match.group(1))
    return profile_id, f"https://{PCU_PROFILE_HOST}/app/ucp/characters/overview/{profile_id}"


def normalize_character_name(raw_name: str) -> str:
    value = unicodedata.normalize("NFKC", raw_name).replace("_", " ")
    normalized = " ".join(value.split())
    if not 3 <= len(normalized) <= 100:
        raise ValueError("El nombre del personaje debe contener entre 3 y 100 caracteres.")
    return normalized


def character_name_key(raw_name: str) -> str:
    normalized = normalize_character_name(raw_name)
    decomposed = unicodedata.normalize("NFKD", normalized.casefold())
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    safe = "".join(character if character.isalnum() else " " for character in without_marks)
    key = " ".join(safe.split())
    if len(key) < 3:
        raise ValueError("Indica un nombre de personaje más específico.")
    return key


def parse_lookup(raw_query: str) -> tuple[int | None, str | None]:
    """Resolve a PCU ID/URL or a normalized character-name lookup."""

    value = raw_query.strip()
    if not value:
        raise ValueError("Indica un ID de PCU, una URL o un nombre de personaje.")
    if value.isdecimal():
        profile_id = int(value)
        if profile_id <= 0:
            raise ValueError("El ID de PCU debe ser positivo.")
        return profile_id, None
    if "://" in value:
        profile_id, _canonical = parse_pcu_profile_url(value)
        return profile_id, None
    return None, character_name_key(value)


def parse_reference(raw_reference: str) -> tuple[str, int]:
    value = raw_reference.strip().upper()
    match = BLACKLIST_REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("Usa una referencia válida, por ejemplo `CCW-00001`.")
    number = int(match.group(2))
    if number <= 0:
        raise ValueError("Usa una referencia válida, por ejemplo `CCW-00001`.")
    return match.group(1), number
