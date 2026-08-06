"""Deterministic public knowledge commands and private Discord navigation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from typing import cast

import discord

from arrest_report_agent.config import PersonaAccessMessages, PersonaFailureMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

LOGGER = logging.getLogger(__name__)

PENAL_CODE_URL = "https://ucp.sarp.es/app/ucp/penalcode"
PROCEDURES_MANUAL_URL = (
    "https://docs.google.com/document/d/1DrhSgzPbdLoe0ahFTVrNPb_zBPuhVeatxW5Hs8ECMHE/"
    "edit?pli=1&tab=t.0#heading=h.thzh3oj2x0rk"
)
KNOWLEDGE_VIEW_TIMEOUT_SECONDS = 180
CHARGES_PER_PAGE = 5
MAX_CHARGE_RESULTS = 50

GUIDE_SECTION_TITLES = {
    "overview": "Visión general",
    "writing_style": "Redacción",
    "narrative_structure": "Estructura narrativa",
    "essential_content": "Contenido esencial",
    "evidence": "Evidencia y anexos",
    "physical_description": "Descripción física",
    "charge_review": "Revisión de cargos",
}
GUIDE_SECTION_DESCRIPTIONS = {
    "overview": "Propósito y criterios generales",
    "writing_style": "Persona, tiempo verbal y claridad",
    "narrative_structure": "Orden recomendado sin plantilla rígida",
    "essential_content": "Datos generales y condicionales",
    "evidence": "Adjuntos, grabaciones y bolsas",
    "physical_description": "Contraste visual con flexibilidad",
    "charge_review": "Validación contra el catálogo vigente",
}
GUIDE_FIELD_TITLES = {
    "purpose": "Propósito",
    "principles": "Principios",
    "expected": "Formato esperado",
    "avoid": "Evitar",
    "recommended_order": "Orden recomendado",
    "flexibility": "Criterio de flexibilidad",
    "generally_required": "Contenido general",
    "conditional": "Contenido condicional",
    "expected_material": "Material habitual",
    "role_environment": "Entorno de roleplay",
    "evidence_bag_reference": "Bolsa de evidencia",
    "format_flexibility": "Flexibilidad de formato",
    "review_rule": "Regla de revisión",
    "rules": "Reglas",
    "rule": "Regla",
    "placement": "Ubicación",
    "suggested_format": "Formato sugerido",
    "review_scope": "Alcance",
}


@dataclass(frozen=True, slots=True)
class FaqEntry:
    key: str
    category: str
    question: str
    answer: tuple[str, ...]
    guide_section: str


FAQ_ENTRIES = (
    FaqEntry(
        key="first_person",
        category="Redacción",
        question="¿En qué persona y tiempo debo escribir?",
        answer=(
            "El reporte debe redactarse en primera persona y en tiempo pasado.",
            "Debe usar lenguaje directo, claro y fácil de entender.",
        ),
        guide_section="writing_style",
    ),
    FaqEntry(
        key="fixed_template",
        category="Redacción",
        question="¿Debo seguir una plantilla exacta?",
        answer=(
            "No. La estructura indicada es un orden recomendado.",
            "No todos los incidentes requieren cada etapa ni el mismo orden; "
            "se evalúan coherencia y suficiencia.",
        ),
        guide_section="narrative_structure",
    ),
    FaqEntry(
        key="required_content",
        category="Narrativa",
        question="¿Qué información debe contener el reporte?",
        answer=(
            "Cuándo ocurrió, quiénes participaron, cómo se inició y dónde tuvo lugar.",
            "Qué ocurrió, por qué se justificó la actuación y cuál fue el resultado final.",
        ),
        guide_section="essential_content",
    ),
    FaqEntry(
        key="911_number",
        category="Narrativa",
        question="¿Siempre debo incluir el número de llamada 911?",
        answer=(
            "No. Puede omitirse si no existe ese dato o si el incidente no se inició mediante una llamada.",
        ),
        guide_section="essential_content",
    ),
    FaqEntry(
        key="recordings",
        category="Evidencia",
        question="¿Debo adjuntar realmente las grabaciones BWC o DICV?",
        answer=(
            "No. Las grabaciones BWC, DICV, Axon u otras menciones equivalentes son entorno narrativo.",
            "No se exige que existan como archivos, enlaces o capturas entre los adjuntos de Discord.",
        ),
        guide_section="evidence",
    ),
    FaqEntry(
        key="evidence_bag",
        category="Evidencia",
        question="¿Cómo debo referenciar una bolsa de evidencia?",
        answer=(
            "Cuando exista evidencia física asociada, indique el número de bolsa en el bloque final.",
            "Formato sugerido: **Se adjunta la referencia a la bolsa de evidencias: #NÚMERO.**",
        ),
        guide_section="evidence",
    ),
    FaqEntry(
        key="physical_mismatch",
        category="Evidencia",
        question="¿Cuándo una descripción física requiere corrección?",
        answer=(
            "Solo ante una contradicción clara, relevante y visible con una característica "
            "afirmada expresamente.",
            "Una diferencia dudosa se trata como observación, no como corrección obligatoria.",
        ),
        guide_section="physical_description",
    ),
    FaqEntry(
        key="charge_validation",
        category="Cargos",
        question="¿Cómo se validan los cargos declarados?",
        answer=(
            "Cada cargo se compara con los hechos narrados y la descripción vigente del catálogo.",
            "No se asumen elementos del delito que no estén narrados o visibles.",
            "Se distingue un cargo incorrecto de un caso donde falta contexto para confirmarlo.",
        ),
        guide_section="charge_review",
    ),
)
FAQ_BY_KEY = {entry.key: entry for entry in FAQ_ENTRIES}


@dataclass(frozen=True, slots=True)
class ChargeSearchEntry:
    id: int
    code: str
    title: str
    category: str
    charge_type: str
    description: str
    sentence_hours: int | float | None
    parole: int | float | None
    fine: int | float | None
    points: int | float | None


class RequesterBoundView(discord.ui.View):
    """Keep private knowledge navigation scoped to its original requester."""

    def __init__(self, requester_id: int, messages: PersonaAccessMessages) -> None:
        super().__init__(timeout=KNOWLEDGE_VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id
        self.messages = messages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            self.messages.other_member_query,
            ephemeral=True,
        )
        return False


class GuideSectionSelect(discord.ui.Select):
    def __init__(self, sections: tuple[str, ...]) -> None:
        options = [
            discord.SelectOption(
                label=GUIDE_SECTION_TITLES.get(section, section.replace("_", " ").title()),
                value=section,
                description=GUIDE_SECTION_DESCRIPTIONS.get(section),
                emoji=_guide_section_emoji(section),
            )
            for section in sections
        ]
        super().__init__(
            placeholder="Seleccione una sección del manual",
            options=options[:25],
            custom_id="general_guide_section",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(GuideNavigationView, self.view)
        view.section = self.values[0]
        await interaction.response.edit_message(embed=view.current_embed, view=view)


class GuideNavigationView(RequesterBoundView):
    def __init__(
        self,
        *,
        requester_id: int,
        guide: ReportGuide,
        display_name: str,
        messages: PersonaAccessMessages,
    ) -> None:
        super().__init__(requester_id, messages)
        self.guide = guide
        self.display_name = display_name
        available = tuple(
            section for section in GUIDE_SECTION_TITLES if section in guide.sections
        )
        self.section = available[0] if available else next(iter(guide.sections))
        self.add_item(GuideSectionSelect(available))

    @property
    def current_embed(self) -> discord.Embed:
        return build_guide_embed(
            self.guide,
            section=self.section,
            display_name=self.display_name,
        )


class FaqQuestionSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label=entry.question[:100],
                value=entry.key,
                description=entry.category,
                emoji=_faq_category_emoji(entry.category),
            )
            for entry in FAQ_ENTRIES
        ]
        super().__init__(
            placeholder="Seleccione una pregunta frecuente",
            options=options,
            custom_id="general_faq_question",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(FaqNavigationView, self.view)
        view.entry = FAQ_BY_KEY[self.values[0]]
        await interaction.response.edit_message(embed=view.current_embed, view=view)


class FaqNavigationView(RequesterBoundView):
    def __init__(
        self,
        *,
        requester_id: int,
        display_name: str,
        messages: PersonaAccessMessages,
    ) -> None:
        super().__init__(requester_id, messages)
        self.display_name = display_name
        self.entry: FaqEntry | None = None
        self.add_item(FaqQuestionSelect())

    @property
    def current_embed(self) -> discord.Embed:
        return build_faq_embed(self.entry, display_name=self.display_name)


class ChargeSearchModal(discord.ui.DesignerModal):
    """Compact deterministic search over the local PCU catalog."""

    def __init__(
        self,
        *,
        catalog: ChargeCatalog,
        requester_id: int,
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
    ) -> None:
        self.catalog = catalog
        self.requester_id = requester_id
        self.display_name = display_name
        self.access_messages = access_messages
        self.failure_messages = failure_messages
        self.query_input = discord.ui.InputText(
            custom_id="charge_search_query",
            placeholder="Ej.: 459, evasión, arma sin licencia",
            min_length=2,
            max_length=100,
            required=True,
        )
        self.category_select = discord.ui.StringSelect(
            custom_id="charge_search_category",
            options=[
                discord.SelectOption(label="Todas las categorías", value="all", default=True),
                *[
                    discord.SelectOption(
                        label=category.title[:100],
                        value=str(category.id),
                        description=f"{len(category.charges)} cargo(s)",
                    )
                    for category in catalog.categories
                    if category.charges
                ],
            ],
        )
        super().__init__(
            discord.ui.Label(
                "Búsqueda",
                self.query_input,
                description="Código, título o términos relacionados.",
            ),
            discord.ui.Label("Categoría", self.category_select),
            title="Consultar el código penal",
            custom_id=f"charge_search:{requester_id}",
            timeout=KNOWLEDGE_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                self.access_messages.other_member_query,
                ephemeral=True,
            )
            return
        query = self.query_input.value.strip()
        raw_category_id = self.category_select.values[0]
        category_id = None if raw_category_id == "all" else int(raw_category_id)
        await interaction.response.defer(ephemeral=True)
        try:
            entries = search_charge_entries(
                self.catalog,
                query=query,
                category_id=category_id,
            )
            view = ChargeSearchPaginationView(
                requester_id=self.requester_id,
                entries=entries,
                query=query,
                display_name=self.display_name,
                messages=self.access_messages,
            )
            await send_charge_search_result(interaction, view)
            log_event(
                LOGGER,
                "info",
                "charge_search_completed",
                guild_id=interaction.guild_id or "direct_message",
                user_id=interaction.user.id,
                category_id=category_id or "all",
                query_length=len(query),
                result_count=len(entries),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "charge_search_failed",
                exc_info=error,
                guild_id=interaction.guild_id or "direct_message",
                user_id=interaction.user.id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                self.failure_messages.charge_query_failure,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class ChargeSearchPaginationView(RequesterBoundView):
    def __init__(
        self,
        *,
        requester_id: int,
        entries: tuple[ChargeSearchEntry, ...],
        query: str,
        display_name: str,
        messages: PersonaAccessMessages,
    ) -> None:
        super().__init__(requester_id, messages)
        self.entries = entries
        self.query = query
        self.display_name = display_name
        self.page = 0
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.entries) / CHARGES_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_charge_search_embed(
            self.entries,
            query=self.query,
            display_name=self.display_name,
            page=self.page,
        )

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    def _synchronize_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1


async def send_charge_search_result(
    interaction: discord.Interaction,
    view: ChargeSearchPaginationView,
) -> None:
    kwargs: dict[str, object] = {
        "embed": view.current_embed,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view.page_count > 1:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)


def search_charge_entries(
    catalog: ChargeCatalog,
    *,
    query: str,
    category_id: int | None,
) -> tuple[ChargeSearchEntry, ...]:
    category_ids = [category_id] if category_id is not None else None
    index = catalog.list_index(
        category_ids=category_ids,
        query=query,
        fields=["code", "title", "category", "description"],
        match_mode="all",
        fuzzy=True,
        limit=MAX_CHARGE_RESULTS,
    )
    raw_results = cast(list[dict[str, object]], index.get("results", []))
    if not raw_results and len(query.split()) > 1:
        index = catalog.list_index(
            category_ids=category_ids,
            query=query,
            fields=["code", "title", "category", "description"],
            match_mode="any",
            fuzzy=True,
            limit=MAX_CHARGE_RESULTS,
        )
        raw_results = cast(list[dict[str, object]], index.get("results", []))

    result_ids = [int(result["id"]) for result in raw_results]
    details_by_id: dict[int, Mapping[str, object]] = {}
    for start in range(0, len(result_ids), 10):
        payload = catalog.get_details(result_ids[start : start + 10])
        details = cast(list[dict[str, object]], payload["charges"])
        details_by_id.update((int(detail["id"]), detail) for detail in details)

    entries: list[ChargeSearchEntry] = []
    for charge_id in result_ids:
        detail = details_by_id.get(charge_id)
        if detail is None:
            continue
        entries.append(
            ChargeSearchEntry(
                id=charge_id,
                code=str(detail["code"]),
                title=str(detail["title"]),
                category=str(detail["category"]),
                charge_type=str(detail["type"]),
                description=str(detail["description"]),
                sentence_hours=_optional_number(detail.get("sentence_hours")),
                parole=_optional_number(detail.get("parole")),
                fine=_optional_number(detail.get("fine")),
                points=_optional_number(detail.get("points")),
            )
        )
    return tuple(entries)


def build_guide_embed(
    guide: ReportGuide,
    *,
    section: str,
    display_name: str,
) -> discord.Embed:
    payload = guide.get([section])
    sections = cast(dict[str, dict[str, object]], payload["sections"])
    content = sections.get(section, {})
    title = GUIDE_SECTION_TITLES.get(section, section.replace("_", " ").title())
    embed = discord.Embed(
        title=f"📘 Guía de reportes · {title}",
        description=(
            "Referencia rápida de la guía local. Consulte el "
            f"[manual oficial]({PROCEDURES_MANUAL_URL}) para el texto completo."
        ),
        color=discord.Color.gold(),
    )
    for key, value in content.items():
        embed.add_field(
            name=GUIDE_FIELD_TITLES.get(key, key.replace("_", " ").title()),
            value=_truncate(_render_guide_value(value), 1024),
            inline=False,
        )
    embed.set_footer(text=f"Guía {guide.version} · {display_name}")
    return embed


def build_faq_embed(
    entry: FaqEntry | None,
    *,
    display_name: str,
) -> discord.Embed:
    if entry is None:
        categories = tuple(dict.fromkeys(item.category for item in FAQ_ENTRIES))
        embed = discord.Embed(
            title="❓ Preguntas frecuentes",
            description=(
                "Seleccione una pregunta para consultar una respuesta breve basada en la guía.\n\n"
                f"**Temas disponibles:** {' · '.join(categories)}"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Referencia determinista · {display_name}")
        return embed

    section_title = GUIDE_SECTION_TITLES.get(entry.guide_section, entry.guide_section)
    embed = discord.Embed(
        title=f"{_faq_category_emoji(entry.category)} {entry.question}",
        description="\n".join(f"• {answer}" for answer in entry.answer),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Referencia",
        value=f"Sección **{section_title}** · [Manual oficial]({PROCEDURES_MANUAL_URL})",
        inline=False,
    )
    embed.set_footer(text=f"FAQ operativo · {display_name}")
    return embed


def build_charge_search_embed(
    entries: tuple[ChargeSearchEntry, ...],
    *,
    query: str,
    display_name: str,
    page: int = 0,
) -> discord.Embed:
    page_count = max(1, ceil(len(entries) / CHARGES_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * CHARGES_PER_PAGE
    page_entries = entries[start : start + CHARGES_PER_PAGE]
    embed = discord.Embed(
        title="⚖️ Consulta de cargos",
        description=(
            f"**Búsqueda:** {discord.utils.escape_markdown(query)}\n"
            f"Catálogo local · [Abrir PCU oficial]({PENAL_CODE_URL})"
        ),
        color=discord.Color.gold(),
    )
    if not page_entries:
        embed.add_field(
            name="Sin coincidencias",
            value="No encontré cargos que coincidan. Pruebe con el código o menos términos.",
            inline=False,
        )
    for entry in page_entries:
        category = discord.utils.escape_markdown(entry.category)
        charge_type = discord.utils.escape_markdown(entry.charge_type)
        description = discord.utils.escape_markdown(entry.description)
        embed.add_field(
            name=(
                f"⚖️ {discord.utils.escape_markdown(entry.code)} · "
                f"{discord.utils.escape_markdown(entry.title)}"
            )[:256],
            value=_truncate(
                f"**Categoría:** {category}\n"
                f"**Tipo:** {charge_type}\n"
                f"{_format_penalties(entry)}\n"
                f"{description}",
                1024,
            ),
            inline=False,
        )
    embed.set_footer(
        text=(
            f"Página {safe_page + 1}/{page_count} · {len(entries)} resultado(s) · "
            f"{display_name}"
        )
    )
    return embed


def _render_guide_value(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(f"• {item}" for item in value)
    if isinstance(value, Mapping):
        lines = []
        for key, nested_value in value.items():
            label = GUIDE_FIELD_TITLES.get(str(key), str(key).replace("_", " ").title())
            lines.append(f"**{label}:** {_render_guide_value(nested_value)}")
        return "\n".join(lines)
    return str(value)


def _format_penalties(entry: ChargeSearchEntry) -> str:
    values = []
    if entry.sentence_hours is not None:
        values.append(f"**Condena:** {_format_number(entry.sentence_hours)} h")
    if entry.parole is not None:
        values.append(f"**Libertad condicional:** {_format_number(entry.parole)}")
    if entry.fine is not None:
        values.append(f"**Multa:** ${_format_number(entry.fine)}")
    if entry.points is not None:
        values.append(f"**Puntos:** {_format_number(entry.points)}")
    return " · ".join(values) if values else "**Sanción:** sin detalle en el catálogo"


def _optional_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:g}"
    return f"{int(value):,}".replace(",", ".")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _guide_section_emoji(section: str) -> str:
    return {
        "overview": "🧭",
        "writing_style": "✍️",
        "narrative_structure": "🧱",
        "essential_content": "📋",
        "evidence": "📦",
        "physical_description": "👤",
        "charge_review": "⚖️",
    }.get(section, "📘")


def _faq_category_emoji(category: str) -> str:
    return {
        "Redacción": "✍️",
        "Narrativa": "📋",
        "Formato": "🧾",
        "Evidencia": "📦",
        "Cargos": "⚖️",
    }.get(category, "❓")
