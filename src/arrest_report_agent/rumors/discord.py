"""Anonymous station rumors posted as ordinary bot messages."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import RumorChannelOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .repository import RumorRepository

LOGGER = logging.getLogger(__name__)
MIN_MESSAGE_LENGTH = 10
MAX_MESSAGE_LENGTH = 1_500
MODAL_TIMEOUT_SECONDS = 600
WARNING = (
    "⚠️ El uso indebido de este comando puede conllevar sanciones. El mensaje se publicará de forma anónima."
)


def normalize_message(value: str) -> str:
    message = value.strip()
    if not MIN_MESSAGE_LENGTH <= len(message) <= MAX_MESSAGE_LENGTH:
        raise ValueError("El mensaje debe tener entre 10 y 1500 caracteres.")
    return message


def build_rumor_embed(message: str, *, published_at: datetime | None = None) -> discord.Embed:
    timestamp = int((published_at or datetime.now(UTC)).timestamp())
    return discord.Embed(
        description=f"🗣️ {discord.utils.escape_mentions(message)}\n\n🕒 <t:{timestamp}:R>",
        color=discord.Color.blurple(),
    )


class RumorCog(commands.Cog):
    def __init__(self, provider: DatabaseConfigProvider, repository: RumorRepository) -> None:
        self.provider = provider
        self.repository = repository

    async def _destination(
        self,
        *,
        guild: discord.Guild | None,
        channel: object,
        user: discord.User | discord.Member | None,
        expected_role_id: int | None = None,
        require_mention_permission: bool = False,
    ) -> tuple[discord.TextChannel, RumorChannelOptions, discord.Role]:
        if guild is None or not isinstance(channel, discord.TextChannel):
            raise ValueError("Este comando solo funciona en los canales de rumor configurados.")
        if not isinstance(user, discord.Member) or not channel.permissions_for(user).view_channel:
            raise ValueError("No tienes acceso a este canal.")
        bundle = await self.provider.cached(guild.id)
        options = bundle.settings.rumor_options
        target = options.target_for_channel(channel.id) if options is not None else None
        if target is None:
            raise ValueError("Este canal no está habilitado para publicar rumores.")
        if expected_role_id is not None and target.role_id != expected_role_id:
            raise ValueError("La configuración de este canal cambió. Abre nuevamente el comando.")
        role = guild.get_role(target.role_id)
        if role is None:
            raise ValueError("El rol asociado a este canal ya no existe.")
        bot_member = guild.me
        if bot_member is None:
            raise ValueError("No pude verificar los permisos del bot en este canal.")
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            raise ValueError("El bot no puede publicar embeds en este canal.")
        if require_mention_permission and not role.mentionable and not permissions.mention_everyone:
            raise ValueError("El bot no puede mencionar el rol asociado a este canal.")
        return channel, target, role

    @discord.slash_command(
        name="rumor",
        description="Publica un rumor anónimo en el canal de la estación",
        contexts={discord.InteractionContextType.guild},
    )
    async def rumor(self, ctx: discord.ApplicationContext) -> None:
        try:
            channel, target, _role = await self._destination(
                guild=ctx.guild,
                channel=ctx.channel,
                user=ctx.author,
            )
            await ctx.send_modal(
                RumorModal(
                    self,
                    requester_id=ctx.author.id,
                    guild_id=channel.guild.id,
                    channel_id=channel.id,
                    role_id=target.role_id,
                )
            )
        except ValueError as error:
            await ctx.respond(str(error), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "rumor_modal_open_failed",
                guild_id=ctx.guild.id if ctx.guild else None,
                channel_id=ctx.channel.id if ctx.channel else None,
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(
                "No pude abrir el formulario. Prueba de nuevo más tarde.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def publish(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        role_id: int,
        raw_message: str,
        mention_role: bool = False,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or interaction.user is None
            or interaction.guild.id != guild_id
            or interaction.channel.id != channel_id
            or interaction.user.id != requester_id
        ):
            await self._private_error(interaction, "Este formulario ya no es válido.")
            return
        try:
            message_text = normalize_message(raw_message)
            channel, target, role = await self._destination(
                guild=interaction.guild,
                channel=interaction.channel,
                user=interaction.user,
                expected_role_id=role_id,
                require_mention_permission=mention_role,
            )
            await interaction.response.defer(ephemeral=True)
            created = await self.repository.reserve(
                interaction_id=interaction.id,
                guild_id=guild_id,
                channel_id=channel_id,
                role_id=target.role_id,
                author_user_id=requester_id,
            )
            if not created:
                with suppress(discord.HTTPException):
                    await interaction.delete_original_response()
                return
            published = await channel.send(
                content=role.mention if mention_role else None,
                embed=build_rumor_embed(message_text),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=[discord.Object(id=role.id)] if mention_role else False,
                    users=False,
                    replied_user=False,
                ),
            )
            try:
                await self.repository.mark_published(interaction.id, published.id)
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "rumor_audit_message_link_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=requester_id,
                    message_id=published.id,
                    interaction_id=interaction.id,
                    exc_info=error,
                    **exception_fields(error),
                )
                try:
                    await published.delete()
                except Exception as delete_error:
                    log_event(
                        LOGGER,
                        "critical",
                        "rumor_unaudited_message_cleanup_failed",
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=requester_id,
                        message_id=published.id,
                        interaction_id=interaction.id,
                        exc_info=delete_error,
                        **exception_fields(delete_error),
                    )
                raise
            log_event(
                LOGGER,
                "info",
                "rumor_published",
                guild_id=guild_id,
                channel_id=channel_id,
                role_id=role.id,
                user_id=requester_id,
                message_id=published.id,
                interaction_id=interaction.id,
                mentioned_role=mention_role,
            )
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
        except ValueError as error:
            await self._private_error(interaction, str(error))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "rumor_publication_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                interaction_id=interaction.id,
                exc_info=error,
                **exception_fields(error),
            )
            await self._private_error(
                interaction,
                f"No pude completar la publicación. Referencia: `{interaction.id}`.",
            )

    @staticmethod
    async def _private_error(interaction: discord.Interaction, description: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                description,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                description,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class RumorModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: RumorCog,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        role_id: int,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.role_id = role_id
        self.message_input = discord.ui.InputText(
            custom_id="rumors:v1:message",
            style=discord.InputTextStyle.long,
            placeholder="Escribe el mensaje que quieres publicar.",
            min_length=MIN_MESSAGE_LENGTH,
            max_length=MAX_MESSAGE_LENGTH,
            required=True,
        )
        self.mention_select = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="rumors:v1:mention",
            options=[
                discord.SelectOption(
                    label="No",
                    value="silent",
                    emoji="🔕",
                    default=True,
                ),
                discord.SelectOption(
                    label="Sí",
                    value="mention",
                    emoji="🔔",
                ),
            ],
            min_values=1,
            max_values=1,
        )
        super().__init__(
            discord.ui.TextDisplay(WARNING),
            discord.ui.Label("Mensaje", self.message_input),
            discord.ui.Label("¿Mencionar al personal?", self.mention_select),
            title="Publicar rumor",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            role_id=self.role_id,
            raw_message=self.message_input.value or "",
            mention_role=next(iter(self.mention_select.values or ()), "silent") == "mention",
        )
