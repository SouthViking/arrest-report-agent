"""Public Discord exercises backed by the deterministic manual quiz catalogue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import cast
from uuid import UUID

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaManualQuizMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    ActiveQuizSessionError,
    NoQuizQuestionsError,
    QuizAnswerMode,
    QuizCategory,
    QuizCooldownError,
    QuizDifficulty,
    QuizDurationSeconds,
    QuizOption,
    QuizQuestion,
)
from .sessions import ManualQuizSessionRegistry

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 180


@dataclass(frozen=True, slots=True)
class CategoryPresentation:
    label: str
    emoji: str


CATEGORY_PRESENTATION = {
    QuizCategory.GENERAL: CategoryPresentation("Conocimiento general", "📚"),
    QuizCategory.RADIO_CODE: CategoryPresentation("Códigos radiales", "📻"),
    QuizCategory.PROCEDURE: CategoryPresentation("Procedimiento", "📋"),
    QuizCategory.SCENARIO: CategoryPresentation("Caso práctico", "🚔"),
    QuizCategory.IDENTIFY_ERROR: CategoryPresentation("Identifique el error", "🔎"),
    QuizCategory.LEGAL: CategoryPresentation("Normativa", "⚖️"),
    QuizCategory.REPORTS: CategoryPresentation("Reportes", "📂"),
}

DIFFICULTY_LABELS = {
    QuizDifficulty.BASIC: "Básica",
    QuizDifficulty.INTERMEDIATE: "Intermedia",
    QuizDifficulty.ADVANCED: "Avanzada",
}

ANSWER_MODE_LABELS = {
    QuizAnswerMode.SINGLE_CHOICE: "Selección única",
    QuizAnswerMode.TRUE_FALSE: "Verdadero o falso",
}

DURATION_LABELS = {
    QuizDurationSeconds.TEN: "10 segundos",
    QuizDurationSeconds.FIFTEEN: "15 segundos",
    QuizDurationSeconds.THIRTY: "30 segundos",
    QuizDurationSeconds.ONE_MINUTE: "1 minuto",
    QuizDurationSeconds.THREE_MINUTES: "3 minutos",
    QuizDurationSeconds.FIVE_MINUTES: "5 minutos",
}


class ManualQuizCog(commands.Cog):
    """Publish one deterministic manual question for an operational member."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        sessions: ManualQuizSessionRegistry,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.default_persona = persona
        self.manual_question.description = persona.messages.interface.commands.manual_question_description

    @discord.slash_command(
        name="pregunta-manual",
        description="Practica el contenido del manual de la facción",
    )
    async def manual_question(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return

        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.manual_quiz
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            await ctx.send_modal(
                ManualQuizConfigurationModal(
                    cog=self,
                    requester_id=ctx.author.id,
                )
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "manual_quiz_command_failed",
                exc_info=error,
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                **exception_fields(error),
            )
            try:
                bundle = await self.provider.cached(ctx.guild.id)
                ui = bundle.persona.profile.messages.interface.manual_quiz
                embed = _alert(ui.alert_title, ui.operation_failed)
            except Exception:
                embed = _generic_alert("No pude preparar la pregunta en este momento.")
            await ctx.respond(embed=embed, ephemeral=True)

    async def publish_question(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        *,
        answer_mode: QuizAnswerMode | None,
        difficulty: QuizDifficulty | None,
        duration: QuizDurationSeconds,
    ) -> None:
        """Resolve the selected catalogue subset and publish one public exercise."""

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        guild_id = guild.id
        user_id = member.id
        question: QuizQuestion | None = None
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.manual_quiz
            if not _member_has_any_role(member, bundle.settings.operational_staff_role_ids):
                await interaction.response.send_message(
                    embed=_alert(ui.alert_title, ui.unauthorized),
                    ephemeral=True,
                )
                return
            try:
                question = await self.sessions.reserve(
                    guild_id,
                    user_id,
                    answer_mode=answer_mode,
                    difficulty=difficulty,
                )
            except ActiveQuizSessionError:
                await interaction.response.send_message(
                    embed=_alert(ui.alert_title, ui.active_question),
                    ephemeral=True,
                )
                return
            except QuizCooldownError as error:
                await interaction.response.send_message(
                    embed=_alert(
                        ui.alert_title,
                        ui.cooldown.format(seconds=error.remaining_seconds),
                    ),
                    ephemeral=True,
                )
                return
            except NoQuizQuestionsError:
                description = (
                    ui.no_questions
                    if answer_mode is None and difficulty is None
                    else ui.no_matching_questions
                )
                await interaction.response.send_message(
                    embed=_alert(ui.alert_title, description),
                    ephemeral=True,
                )
                return

            view = ManualQuizView(
                publisher_id=user_id,
                guild_id=guild_id,
                question=question,
                ui=ui,
                sessions=self.sessions,
                duration_seconds=duration,
            )
            await interaction.response.send_message(
                embed=build_question_embed(
                    question,
                    ui,
                    publisher_name=member.display_name,
                    publisher_avatar_url=str(member.display_avatar.url),
                    duration_seconds=duration,
                ),
                view=view,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            with suppress(discord.HTTPException):
                view.message = await interaction.original_response()
            log_event(
                LOGGER,
                "info",
                "manual_quiz_question_published",
                guild_id=guild_id,
                channel_id=interaction.channel.id if interaction.channel else "unknown",
                user_id=user_id,
                question_id=str(question.id),
                answer_mode=question.answer_mode.value,
                category=question.category.value,
                difficulty=question.difficulty.value,
                duration_seconds=int(duration),
            )
        except Exception as error:
            if question is not None:
                await self.sessions.cancel(guild_id, user_id, question.id)
            log_event(
                LOGGER,
                "error",
                "manual_quiz_publish_failed",
                exc_info=error,
                guild_id=guild_id,
                user_id=user_id,
                **exception_fields(error),
            )
            try:
                bundle = await self.provider.cached(guild_id)
                ui = bundle.persona.profile.messages.interface.manual_quiz
                embed = _alert(ui.alert_title, ui.operation_failed)
            except Exception:
                embed = _generic_alert("No pude preparar la pregunta en este momento.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)


class ManualQuizConfigurationModal(discord.ui.DesignerModal):
    """Collect bounded catalogue filters without exposing inline slash arguments."""

    def __init__(self, *, cog: ManualQuizCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.difficulty = discord.ui.StringSelect(
            custom_id="manual-quiz:v2:difficulty",
            options=[
                discord.SelectOption(label="Aleatoria", value="any", default=True),
                *[
                    discord.SelectOption(label=label, value=value.value)
                    for value, label in DIFFICULTY_LABELS.items()
                ],
            ],
        )
        self.answer_mode = discord.ui.StringSelect(
            custom_id="manual-quiz:v2:answer-mode",
            options=[
                discord.SelectOption(label="Aleatorio", value="any", default=True),
                *[
                    discord.SelectOption(label=label, value=value.value)
                    for value, label in ANSWER_MODE_LABELS.items()
                ],
            ],
        )
        self.duration = discord.ui.StringSelect(
            custom_id="manual-quiz:v2:duration",
            options=[
                discord.SelectOption(
                    label=label,
                    value=str(int(value)),
                    default=value is QuizDurationSeconds.THREE_MINUTES,
                )
                for value, label in DURATION_LABELS.items()
            ],
        )
        super().__init__(
            discord.ui.Label("Dificultad", self.difficulty),
            discord.ui.Label("Tipo de pregunta", self.answer_mode),
            discord.ui.Label("Tiempo para responder", self.duration),
            title="Configurar pregunta",
            custom_id=f"manual-quiz:v2:configure:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.id != self.requester_id:
            ui = self.cog.default_persona.messages.interface.manual_quiz
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        raw_difficulty = self.difficulty.values[0]
        raw_answer_mode = self.answer_mode.values[0]
        await self.cog.publish_question(
            interaction,
            member,
            difficulty=(None if raw_difficulty == "any" else QuizDifficulty(raw_difficulty)),
            answer_mode=(None if raw_answer_mode == "any" else QuizAnswerMode(raw_answer_mode)),
            duration=QuizDurationSeconds(int(self.duration.values[0])),
        )


class ManualQuizChoiceSelect(discord.ui.Select):
    def __init__(self, question: QuizQuestion, placeholder: str) -> None:
        super().__init__(
            placeholder=placeholder,
            custom_id=f"manual-quiz:v1:answer:{question.id}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_option_label(option.position, option.text)[:100],
                    value=str(option.id),
                )
                for option in question.options
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(ManualQuizView, self.view)
        await view.submit(interaction, UUID(self.values[0]))


class ManualQuizAnswerButton(discord.ui.Button["ManualQuizView"]):
    def __init__(self, option: QuizOption) -> None:
        super().__init__(
            label=option.text[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"manual-quiz:v1:answer:{option.id}",
        )
        self.option_id = option.id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(ManualQuizView, self.view)
        await view.submit(interaction, self.option_id)


class ManualQuizView(discord.ui.View):
    def __init__(
        self,
        *,
        publisher_id: int,
        guild_id: int,
        question: QuizQuestion,
        ui: PersonaManualQuizMessages,
        sessions: ManualQuizSessionRegistry,
        duration_seconds: QuizDurationSeconds,
    ) -> None:
        super().__init__(timeout=float(duration_seconds), disable_on_timeout=True)
        self.publisher_id = publisher_id
        self.guild_id = guild_id
        self.question = question
        self.ui = ui
        self.sessions = sessions
        self.duration_seconds = duration_seconds
        self.expires_at = monotonic() + int(duration_seconds)
        self._answer_lock = asyncio.Lock()
        self._answered = False
        if question.answer_mode is QuizAnswerMode.SINGLE_CHOICE:
            self.add_item(ManualQuizChoiceSelect(question, ui.selector_placeholder))
        elif question.answer_mode is QuizAnswerMode.TRUE_FALSE:
            for option in question.options:
                self.add_item(ManualQuizAnswerButton(option))
        else:  # defensive: the repository currently filters these modes out
            raise ValueError(f"unsupported manual quiz answer mode: {question.answer_mode}")

    async def submit(self, interaction: discord.Interaction, option_id: UUID) -> None:
        async with self._answer_lock:
            if self._answered:
                await interaction.response.send_message(
                    embed=_alert(self.ui.alert_title, self.ui.already_answered),
                    ephemeral=True,
                )
                return
            if monotonic() >= self.expires_at:
                await self._expire_from_interaction(interaction)
                return
            respondent = interaction.user
            if respondent is None:
                await interaction.response.send_message(
                    embed=_alert(self.ui.alert_title, self.ui.operation_failed),
                    ephemeral=True,
                )
                return
            option = self.question.option(option_id)
            if option is None:
                await interaction.response.send_message(
                    embed=_alert(self.ui.alert_title, self.ui.operation_failed),
                    ephemeral=True,
                )
                return
            self._answered = True
            self.disable_all_items()
            try:
                await interaction.response.edit_message(
                    embed=build_result_embed(
                        self.question,
                        option,
                        self.ui,
                        respondent_id=respondent.id,
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log_event(
                    LOGGER,
                    "info",
                    "manual_quiz_answered",
                    guild_id=self.guild_id,
                    publisher_user_id=self.publisher_id,
                    respondent_user_id=respondent.id,
                    question_id=str(self.question.id),
                    correct=option.is_correct,
                )
            finally:
                await self.sessions.release(
                    self.guild_id,
                    self.publisher_id,
                    self.question.id,
                )
                self.stop()

    async def on_timeout(self) -> None:
        async with self._answer_lock:
            if self._answered:
                return
            self._answered = True
            self.disable_all_items()
            try:
                if self.message is not None:
                    await self.message.edit(
                        embed=build_timeout_embed(
                            self.question,
                            self.ui,
                            duration_seconds=self.duration_seconds,
                            publisher_id=self.publisher_id,
                        ),
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await super().on_timeout()
                log_event(
                    LOGGER,
                    "info",
                    "manual_quiz_timed_out",
                    guild_id=self.guild_id,
                    publisher_user_id=self.publisher_id,
                    question_id=str(self.question.id),
                    duration_seconds=int(self.duration_seconds),
                )
            except discord.HTTPException as error:
                log_event(
                    LOGGER,
                    "warning",
                    "manual_quiz_timeout_edit_failed",
                    exc_info=error,
                    guild_id=self.guild_id,
                    publisher_user_id=self.publisher_id,
                    question_id=str(self.question.id),
                    **exception_fields(error),
                )
            finally:
                await self.sessions.release(
                    self.guild_id,
                    self.publisher_id,
                    self.question.id,
                )
                self.stop()

    async def _expire_from_interaction(self, interaction: discord.Interaction) -> None:
        self._answered = True
        self.disable_all_items()
        try:
            await interaction.response.edit_message(
                embed=build_timeout_embed(
                    self.question,
                    self.ui,
                    duration_seconds=self.duration_seconds,
                    publisher_id=self.publisher_id,
                ),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            await self.sessions.release(
                self.guild_id,
                self.publisher_id,
                self.question.id,
            )
            self.stop()


def build_question_embed(
    question: QuizQuestion,
    ui: PersonaManualQuizMessages,
    *,
    publisher_name: str,
    publisher_avatar_url: str | None = None,
    duration_seconds: QuizDurationSeconds = QuizDurationSeconds.THREE_MINUTES,
) -> discord.Embed:
    presentation = CATEGORY_PRESENTATION[question.category]
    embed = discord.Embed(
        title=f"{presentation.emoji} Pregunta del manual · {presentation.label}",
        description=ui.question_intro,
        color=discord.Color.blurple(),
    )
    author_name = f"Publicada por {publisher_name}"
    if publisher_avatar_url:
        embed.set_author(name=author_name, icon_url=publisher_avatar_url)
    else:
        embed.set_author(name=author_name)
    embed.add_field(name="Pregunta", value=question.prompt, inline=False)
    embed.add_field(
        name="Alternativas",
        value="\n".join(
            f"**{_option_letter(option.position)}.** {option.text}" for option in question.options
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"Dificultad {DIFFICULTY_LABELS[question.difficulty]} · "
            f"Respuesta abierta durante {DURATION_LABELS[duration_seconds]}"
        )
    )
    return embed


def build_timeout_embed(
    question: QuizQuestion,
    ui: PersonaManualQuizMessages,
    *,
    duration_seconds: QuizDurationSeconds,
    publisher_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="⌛ Tiempo agotado",
        description=_stable_pool_choice(ui.expired_messages, question.id, publisher_id),
        color=discord.Color.orange(),
    )
    embed.add_field(name="Pregunta", value=question.prompt, inline=False)
    embed.add_field(
        name="Respuesta correcta",
        value="\n".join(option.text for option in question.correct_options),
        inline=False,
    )
    embed.add_field(name="Explicación", value=question.explanation, inline=False)
    embed.add_field(name="Referencia", value=question.source_reference, inline=False)
    embed.set_footer(text=(f"Tiempo disponible: {DURATION_LABELS[duration_seconds]} · ejercicio sin puntaje"))
    return embed


def build_result_embed(
    question: QuizQuestion,
    selected: QuizOption,
    ui: PersonaManualQuizMessages,
    *,
    respondent_id: int,
) -> discord.Embed:
    correct = selected.is_correct
    pool = ui.correct_messages if correct else ui.incorrect_messages
    description = _stable_pool_choice(pool, question.id, respondent_id)
    embed = discord.Embed(
        title="✅ Respuesta correcta" if correct else "❌ Respuesta incorrecta",
        description=description,
        color=discord.Color.green() if correct else discord.Color.red(),
    )
    embed.add_field(name="Pregunta", value=question.prompt, inline=False)
    embed.add_field(name="Respondió", value=f"<@{respondent_id}>", inline=False)
    embed.add_field(name="Respuesta seleccionada", value=selected.text, inline=False)
    if not correct:
        embed.add_field(
            name="Respuesta correcta",
            value="\n".join(option.text for option in question.correct_options),
            inline=False,
        )
    embed.add_field(name="Explicación", value=question.explanation, inline=False)
    embed.add_field(name="Referencia", value=question.source_reference, inline=False)
    embed.set_footer(text="Ejercicio de práctica · no registra puntaje")
    return embed


def _stable_pool_choice(pool: tuple[str, ...], question_id: UUID, user_id: int) -> str:
    digest = hashlib.sha256(f"{question_id}:{user_id}".encode()).digest()
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


def _option_letter(position: int) -> str:
    return chr(ord("A") + position)


def _option_label(position: int, text: str) -> str:
    return f"{_option_letter(position)}. {text}"


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Entrenamiento no disponible", description)
