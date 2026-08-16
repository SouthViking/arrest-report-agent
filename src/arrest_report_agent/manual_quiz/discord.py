"""Public Discord exercises backed by the deterministic manual quiz catalogue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
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
    QuizOption,
    QuizQuestion,
)
from .sessions import ManualQuizSessionRegistry

LOGGER = logging.getLogger(__name__)
QUESTION_TIMEOUT_SECONDS = 180


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
        self.manual_question.description = (
            persona.messages.interface.commands.manual_question_description
        )

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

        guild_id = ctx.guild.id
        user_id = ctx.author.id
        question: QuizQuestion | None = None
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.manual_quiz
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            try:
                question = await self.sessions.reserve(guild_id, user_id)
            except ActiveQuizSessionError:
                await ctx.respond(
                    embed=_alert(ui.alert_title, ui.active_question),
                    ephemeral=True,
                )
                return
            except QuizCooldownError as error:
                await ctx.respond(
                    embed=_alert(
                        ui.alert_title,
                        ui.cooldown.format(seconds=error.remaining_seconds),
                    ),
                    ephemeral=True,
                )
                return
            except NoQuizQuestionsError:
                await ctx.respond(
                    embed=_alert(ui.alert_title, ui.no_questions),
                    ephemeral=True,
                )
                return

            view = ManualQuizView(
                publisher_id=user_id,
                guild_id=guild_id,
                question=question,
                ui=ui,
                sessions=self.sessions,
            )
            await ctx.respond(
                embed=build_question_embed(
                    question,
                    ui,
                    publisher_name=ctx.author.display_name,
                    publisher_avatar_url=str(ctx.author.display_avatar.url),
                ),
                view=view,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "manual_quiz_question_published",
                guild_id=guild_id,
                channel_id=ctx.channel.id if ctx.channel else "unknown",
                user_id=user_id,
                question_id=str(question.id),
                answer_mode=question.answer_mode.value,
                category=question.category.value,
            )
        except Exception as error:
            if question is not None:
                await self.sessions.cancel(guild_id, user_id, question.id)
            log_event(
                LOGGER,
                "error",
                "manual_quiz_command_failed",
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
            await ctx.respond(embed=embed, ephemeral=True)


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
    ) -> None:
        super().__init__(timeout=QUESTION_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.publisher_id = publisher_id
        self.guild_id = guild_id
        self.question = question
        self.ui = ui
        self.sessions = sessions
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
                        respondent_name=respondent.display_name,
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
        await self.sessions.release(
            self.guild_id,
            self.publisher_id,
            self.question.id,
        )
        await super().on_timeout()


def build_question_embed(
    question: QuizQuestion,
    ui: PersonaManualQuizMessages,
    *,
    publisher_name: str,
    publisher_avatar_url: str | None = None,
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
            f"**{_option_letter(option.position)}.** {option.text}"
            for option in question.options
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Dificultad {DIFFICULTY_LABELS[question.difficulty]} · Respuesta abierta"
    )
    return embed


def build_result_embed(
    question: QuizQuestion,
    selected: QuizOption,
    ui: PersonaManualQuizMessages,
    *,
    respondent_id: int,
    respondent_name: str,
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
    embed.add_field(
        name=f"Respuesta de {respondent_name}",
        value=selected.text,
        inline=False,
    )
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
