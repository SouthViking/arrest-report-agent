"""Domain models and normalization for structured faction blacklists."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arrest_report_agent.config import PcuProfileKind

PCU_PROFILE_HOST = "ucp.sarp.es"
PCU_PROFILE_PATHS = {
    PcuProfileKind.CHARACTER: re.compile(r"^/app/ucp/characters/overview/([1-9][0-9]*)/?$"),
    PcuProfileKind.ACCOUNT: re.compile(r"^/app/ucp/accounts/overview/([1-9][0-9]*)/?$"),
}
PCU_PROFILE_SEGMENTS = {
    PcuProfileKind.CHARACTER: "characters",
    PcuProfileKind.ACCOUNT: "accounts",
}
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
    profile_kind: PcuProfileKind
    pcu_profile_ids: tuple[int, ...] = Field(min_length=1, max_length=5)
    pcu_urls: tuple[str, ...] = Field(min_length=1, max_length=5)
    subject_label: str = Field(min_length=1, max_length=30)
    subject_name: str = Field(min_length=3, max_length=100)
    subject_name_key: str = Field(min_length=3, max_length=100)
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
        if len(self.pcu_profile_ids) != len(self.pcu_urls):
            raise ValueError("blacklist PCU profile IDs and URLs must have equal lengths")
        if len(set(self.pcu_profile_ids)) != len(self.pcu_profile_ids):
            raise ValueError("blacklist PCU profile IDs must be unique")
        for profile_id, url in zip(self.pcu_profile_ids, self.pcu_urls, strict=True):
            parsed_id, canonical = parse_pcu_profile_url(
                url,
                expected_kind=self.profile_kind,
            )
            if parsed_id != profile_id or canonical != url:
                raise ValueError("blacklist PCU profiles must use canonical matching URLs")
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
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"

    @property
    def pcu_profile_id(self) -> int:
        return self.pcu_profile_ids[0]

    @property
    def pcu_url(self) -> str:
        return self.pcu_urls[0]

    @property
    def character_name(self) -> str:
        """Compatibility alias for callers predating generic blacklist subjects."""

        return self.subject_name

    @property
    def character_name_key(self) -> str:
        return self.subject_name_key


def parse_pcu_profile_url(
    raw_url: str,
    *,
    expected_kind: PcuProfileKind = PcuProfileKind.CHARACTER,
) -> tuple[int, str]:
    """Validate one exact SARP PCU profile URL and return its ID and canonical form."""

    kind, profile_id, canonical = parse_any_pcu_profile_url(raw_url)
    if kind is not expected_kind:
        segment = PCU_PROFILE_SEGMENTS[expected_kind]
        raise ValueError(f"La URL debe usar /app/ucp/{segment}/overview/<id>.")
    return profile_id, canonical


def parse_any_pcu_profile_url(raw_url: str) -> tuple[PcuProfileKind, int, str]:
    """Validate either an account or character PCU profile URL."""

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
    for kind, pattern in PCU_PROFILE_PATHS.items():
        match = pattern.fullmatch(parsed.path)
        if match is None:
            continue
        profile_id = int(match.group(1))
        segment = PCU_PROFILE_SEGMENTS[kind]
        canonical = f"https://{PCU_PROFILE_HOST}/app/ucp/{segment}/overview/{profile_id}"
        return kind, profile_id, canonical
    raise ValueError("La URL debe corresponder a /characters/overview/<id> o /accounts/overview/<id>.")


def parse_pcu_profile_urls(
    raw_urls: str,
    *,
    expected_kind: PcuProfileKind,
    max_profiles: int,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Validate one or more whitespace-separated PCU URLs for a configured type."""

    values = raw_urls.split()
    if not values:
        raise ValueError("Indica al menos una URL de PCU.")
    if len(values) > max_profiles:
        raise ValueError(f"Este tipo admite un máximo de {max_profiles} perfiles de PCU.")
    parsed = tuple(parse_pcu_profile_url(value, expected_kind=expected_kind) for value in values)
    profile_ids = tuple(item[0] for item in parsed)
    urls = tuple(item[1] for item in parsed)
    if len(set(profile_ids)) != len(profile_ids):
        raise ValueError("No repitas la misma cuenta o personaje de PCU.")
    return profile_ids, urls


def normalize_subject_name(raw_name: str) -> str:
    value = unicodedata.normalize("NFKC", raw_name).replace("_", " ")
    normalized = " ".join(value.split())
    if not 3 <= len(normalized) <= 100:
        raise ValueError("El nombre debe contener entre 3 y 100 caracteres.")
    return normalized


def subject_name_key(raw_name: str) -> str:
    normalized = normalize_subject_name(raw_name)
    decomposed = unicodedata.normalize("NFKD", normalized.casefold())
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    safe = "".join(character if character.isalnum() else " " for character in without_marks)
    key = " ".join(safe.split())
    if len(key) < 3:
        raise ValueError("Indica un nombre más específico.")
    return key


def normalize_character_name(raw_name: str) -> str:
    return normalize_subject_name(raw_name)


def character_name_key(raw_name: str) -> str:
    return subject_name_key(raw_name)


def parse_lookup(
    raw_query: str,
) -> tuple[PcuProfileKind | None, int | None, str | None]:
    """Resolve a typed PCU URL, an untyped PCU ID or a normalized name lookup."""

    value = raw_query.strip()
    if not value:
        raise ValueError("Indica un ID de PCU, una URL o un nombre de personaje.")
    if value.isdecimal():
        profile_id = int(value)
        if profile_id <= 0:
            raise ValueError("El ID de PCU debe ser positivo.")
        return None, profile_id, None
    if "://" in value:
        kind, profile_id, _canonical = parse_any_pcu_profile_url(value)
        return kind, profile_id, None
    return None, None, subject_name_key(value)


def parse_reference(raw_reference: str) -> tuple[str, int]:
    value = raw_reference.strip().upper()
    match = BLACKLIST_REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("Usa una referencia válida, por ejemplo `CCW-00001`.")
    number = int(match.group(2))
    if number <= 0:
        raise ValueError("Usa una referencia válida, por ejemplo `CCW-00001`.")
    return match.group(1), number
