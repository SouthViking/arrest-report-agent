"""Validated, portable options for installation-specific public references."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

Snowflake = Annotated[int, Field(gt=0, le=2**64 - 1, strict=True)]


class InstanceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def public_https_url(value: HttpUrl) -> HttpUrl:
    if value.scheme != "https" or value.username is not None or value.password is not None:
        raise ValueError("Use an HTTPS URL without embedded credentials")
    return value


class SupportOptions(InstanceOptions):
    url: HttpUrl

    _url = field_validator("url")(public_https_url)


class DedeoOptions(InstanceOptions):
    protected_user_ids: tuple[Snowflake, ...] = Field(default=(), max_length=25)

    @field_validator("protected_user_ids")
    @classmethod
    def unique_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)):
            raise ValueError("protected_user_ids must be unique")
        return values


class KnowledgeLinksOptions(InstanceOptions):
    procedures_manual_url: HttpUrl | None = None
    penal_code_url: HttpUrl | None = None

    @field_validator("procedures_manual_url", "penal_code_url")
    @classmethod
    def public_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        return public_https_url(value) if value is not None else None


class ReleaseRoutingOptions(InstanceOptions):
    internal_channel_ids: tuple[Snowflake, ...] = Field(default=(), max_length=25)

    @field_validator("internal_channel_ids")
    @classmethod
    def unique_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)):
            raise ValueError("internal_channel_ids must be unique")
        return values


class TeamSpeakOptions(InstanceOptions):
    address: str = Field(min_length=1, max_length=200)

    @field_validator("address")
    @classmethod
    def plain_address(cls, value: str) -> str:
        if any(char.isspace() for char in value) or any(char in value for char in "<>`/@"):
            raise ValueError("Use a TeamSpeak hostname or IP, optionally followed by a port")
        return value
