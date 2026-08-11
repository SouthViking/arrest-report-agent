"""Idempotent synchronization of the selected persona with Discord's bot profile."""

from __future__ import annotations

import asyncio
import logging

import discord

from arrest_report_agent.config import LoadedPersona
from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)


class DiscordProfileSynchronizer:
    """Apply global and guild profile fields once and retry only after failures."""

    _GLOBAL_PROFILE_FIELDS = frozenset({"username", "avatar", "banner"})

    def __init__(
        self,
        bot: discord.Bot,
        persona: LoadedPersona,
        *,
        global_banner_bytes: bytes,
    ) -> None:
        self.bot = bot
        self.persona = persona
        self.global_banner_bytes = global_banner_bytes
        self._lock = asyncio.Lock()
        self._synchronized_global_fields: set[str] = set()
        self._guild_profile_ids: set[int] = set()

    @property
    def global_profile_synchronized(self) -> bool:
        return self._synchronized_global_fields >= self._GLOBAL_PROFILE_FIELDS

    async def synchronize(self) -> None:
        """Synchronize profile fields without preventing the bot from becoming ready."""

        async with self._lock:
            if not self.global_profile_synchronized:
                await self._synchronize_global_profile()
            await self._synchronize_guild_profiles()

    async def select_persona(self, persona: LoadedPersona) -> None:
        """Switch the desired profile and safely retry every managed field."""

        async with self._lock:
            if persona.profile.id == self.persona.profile.id and (
                persona.profile.revision == self.persona.profile.revision
            ):
                await self._synchronize_global_profile()
                await self._synchronize_guild_profiles()
                return
            self.persona = persona
            self._synchronized_global_fields.clear()
            self._guild_profile_ids.clear()
            await self._synchronize_global_profile()
            await self._synchronize_guild_profiles()

    async def _synchronize_global_profile(self) -> None:
        bot_user = self.bot.user
        profile = self.persona.profile
        if bot_user is None:
            log_event(
                LOGGER,
                "warning",
                "discord_profile_sync_deferred",
                persona_id=profile.id,
                persona_revision=profile.revision,
                reason="bot_user_unavailable",
            )
            return

        if bot_user.name == profile.discord.username:
            self._synchronized_global_fields.add("username")

        await self._synchronize_global_field(
            bot_user,
            field="username",
            value=profile.discord.username,
        )
        await self._synchronize_global_field(
            bot_user,
            field="avatar",
            value=self.persona.avatar_bytes,
        )
        await self._synchronize_global_field(
            bot_user,
            field="banner",
            value=self.global_banner_bytes,
        )

    async def _synchronize_global_field(
        self,
        bot_user: discord.ClientUser,
        *,
        field: str,
        value: str | bytes,
    ) -> None:
        if field in self._synchronized_global_fields:
            return

        log_event(
            LOGGER,
            "info",
            "discord_profile_field_sync_started",
            managed_field=field,
            persona_id=self.persona.profile.id,
            persona_revision=self.persona.profile.revision,
        )
        try:
            await bot_user.edit(**{field: value})
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "discord_profile_field_sync_failed",
                exc_info=error,
                managed_field=field,
                persona_id=self.persona.profile.id,
                persona_revision=self.persona.profile.revision,
                **exception_fields(error),
            )
            return

        self._synchronized_global_fields.add(field)
        log_event(
            LOGGER,
            "info",
            "discord_profile_field_sync_completed",
            managed_field=field,
            persona_id=self.persona.profile.id,
            persona_revision=self.persona.profile.revision,
        )

    async def _synchronize_guild_profiles(self) -> None:
        profile = self.persona.profile
        nickname = profile.discord.nickname
        for guild in self.bot.guilds:
            if guild.id in self._guild_profile_ids:
                continue
            member = guild.me
            if member is None:
                continue
            managed_fields = ["bio"]
            edit_fields: dict[str, str] = {"bio": profile.discord.bio}
            if nickname is not None and member.nick != nickname:
                managed_fields.insert(0, "nickname")
                edit_fields["nick"] = nickname
            try:
                await member.edit(**edit_fields)
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "discord_guild_profile_sync_failed",
                    exc_info=error,
                    guild_id=guild.id,
                    managed_fields=managed_fields,
                    persona_id=profile.id,
                    persona_revision=profile.revision,
                    **exception_fields(error),
                )
                continue
            self._guild_profile_ids.add(guild.id)
            log_event(
                LOGGER,
                "info",
                "discord_guild_profile_sync_completed",
                guild_id=guild.id,
                managed_fields=managed_fields,
                persona_id=profile.id,
                persona_revision=profile.revision,
            )
