from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.infrastructure.sarp import (
    SarpArrestPage,
    SarpCharacterPossessions,
    SarpCharacterPreview,
    SarpCharacterProperty,
    SarpCharacterSearchResult,
    SarpCharacterVehicle,
    SarpFine,
    SarpFinePage,
    SarpPoliceCharge,
    SarpWarrant,
    SarpWarrantPage,
)
from arrest_report_agent.mdc import (
    MdcCharacterNotFoundError,
    MdcCog,
    MdcRecord,
    MdcSearchModal,
    MdcSection,
    MdcService,
    MdcVisibility,
    build_mdc_embed,
    build_mdc_info_embed,
    character_name_key,
    normalize_character_name,
)


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _page_client(*, fail_fines: bool = False):
    charge = SarpPoliceCharge(
        charge_id=5,
        code="CH-4102",
        charge="Evasión",
        type="Felony",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.searches: list[str] = []
            self.detail_calls = 0

        async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]:
            self.searches.append(name)
            return [
                SarpCharacterSearchResult(id=44, name="Terry_McKenzie"),
                SarpCharacterSearchResult(id=45, name="Terry_McKenzie_Jr"),
            ]

        async def get_character_preview(self, character_id: int) -> SarpCharacterPreview:
            self.detail_calls += 1
            assert character_id == 44
            return SarpCharacterPreview(id=44, name="Terry_McKenzie", jailed=False)

        async def get_character_possessions(self, character_id: int) -> SarpCharacterPossessions:
            self.detail_calls += 1
            return SarpCharacterPossessions(
                vehicles=[
                    SarpCharacterVehicle(
                        id=1,
                        model=596,
                        model_name="Premier",
                        plate="LS-1234",
                    )
                ],
                properties=[
                    SarpCharacterProperty(id=2, name="Vivienda", address="Dillimore")
                ],
            )

        async def list_unpaid_fines(
            self,
            character_id: int,
            *,
            page: int = 1,
            limit: int = 100,
        ) -> SarpFinePage:
            self.detail_calls += 1
            if fail_fines:
                raise RuntimeError("rate limited")
            return SarpFinePage(
                items=[
                    SarpFine(
                        id=3,
                        character_id=character_id,
                        fine="Conducción temeraria",
                        cost=1500,
                        is_paid=0,
                    )
                ],
                total=1,
                page=page,
                limit=limit,
                pages=1,
            )

        async def list_active_warrants(
            self,
            character_id: int,
            *,
            page: int = 1,
            limit: int = 25,
        ) -> SarpWarrantPage:
            self.detail_calls += 1
            return SarpWarrantPage(
                items=[
                    SarpWarrant(
                        id=4,
                        suspect_id=character_id,
                        description="Detención pendiente",
                        type=["arrest"],
                        status=1,
                        charges=[charge],
                    )
                ],
                total=1,
                page=page,
                limit=limit,
                pages=1,
            )

        async def list_arrests(
            self,
            character_id: int,
            *,
            page: int = 1,
            limit: int = 3,
        ) -> SarpArrestPage:
            self.detail_calls += 1
            return SarpArrestPage(
                items=[
                    {
                        "id": 6,
                        "suspect_id": character_id,
                        "created_at": "2026-08-01T12:00:00Z",
                        "charges": [charge.model_dump()],
                    }
                ],
                total=1,
                page=page,
                limit=limit,
                pages=1,
            )

    return FakeClient()


@pytest.mark.parametrize(
    ("raw", "normalized"),
    (
        ("  Terry__McKenzie ", "Terry McKenzie"),
        ("Terry   McKenzie", "Terry McKenzie"),
        ("Ｔｅｒｒｙ McKenzie", "Terry McKenzie"),
    ),
)
def test_mdc_name_normalization_is_exact_but_case_and_separator_insensitive(
    raw: str,
    normalized: str,
) -> None:
    assert normalize_character_name(raw) == normalized
    assert character_name_key(raw) == character_name_key(normalized.upper())


async def test_mdc_service_projects_exact_ic_safe_record_and_caches_it() -> None:
    client = _page_client()
    service = MdcService(client, cache_seconds=30)

    first = await service.lookup_exact("terry mckenzie")
    second = await service.lookup_exact("Terry_McKenzie")

    assert first is second
    assert client.searches == ["terry_mckenzie"]
    assert client.detail_calls == 5
    assert first.name == "Terry McKenzie"
    assert first.vehicles[0].plate == "LS-1234"
    assert first.pending_fine_amount == 1500
    assert first.active_warrants[0].charges[0].name == "Evasión"
    assert first.arrest_count == 1
    assert first.unavailable_sections == ()
    assert not hasattr(first, "id")
    assert not hasattr(first, "level")


async def test_mdc_service_keeps_other_sections_when_one_api_request_fails() -> None:
    service = MdcService(_page_client(fail_fines=True), cache_seconds=0)

    record = await service.lookup_exact("Terry McKenzie")

    assert record.vehicles
    assert record.active_warrants
    assert record.pending_fine_count is None
    assert record.unavailable_sections == (MdcSection.FINES,)


async def test_mdc_service_does_not_fetch_details_without_an_exact_match() -> None:
    client = _page_client()
    service = MdcService(client)

    with pytest.raises(MdcCharacterNotFoundError):
        await service.lookup_exact("Terry")

    assert client.detail_calls == 0


def test_mdc_embed_is_public_safe_and_marks_partial_sections(persona) -> None:
    record = MdcRecord(
        name="Terry McKenzie",
        incarcerated=False,
        pending_fine_count=None,
        unavailable_sections=(MdcSection.FINES,),
    )

    embed = build_mdc_embed(record, persona.messages.interface.mdc)
    rendered = "\n".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )

    assert embed.title == "🖥️ MDC · Terry McKenzie"
    assert "Información no disponible" in rendered
    assert "multas pendientes" in rendered
    assert "ID" not in rendered
    assert "nivel" not in rendered.lower()
    assert embed.image is None


async def test_mdc_service_normalizes_null_property_values() -> None:
    client = _page_client()

    async def get_character_possessions(_character_id: int) -> SarpCharacterPossessions:
        return SarpCharacterPossessions(
            properties=[
                SarpCharacterProperty(
                    id=2,
                    name=" NULL ",
                    address="Alamosa Avenue, Marina, Los Santos 313, San Andreas",
                )
            ]
        )

    client.get_character_possessions = get_character_possessions
    record = await MdcService(client, cache_seconds=0).lookup_exact("Terry McKenzie")

    assert record.properties[0].name is None
    assert record.properties[0].address == "Alamosa Avenue, Marina, Los Santos 313, San Andreas"


def test_mdc_embed_omits_null_property_name(persona) -> None:
    record = MdcRecord(
        name="Terry McKenzie",
        properties=(
            {
                "name": None,
                "address": "Alamosa Avenue, Marina, Los Santos 313, San Andreas",
            },
        ),
    )

    embed = build_mdc_embed(record, persona.messages.interface.mdc)
    properties = next(field.value for field in embed.fields if field.name.startswith("🏠"))

    assert properties == "• Alamosa Avenue, Marina, Los Santos 313, San Andreas"
    assert "NULL" not in properties


def test_mdc_info_embed_explains_usage_visibility_and_results(persona) -> None:
    ui = persona.messages.interface.mdc

    embed = build_mdc_info_embed(ui)

    assert embed.title == "🖥️ Cómo usar el MDC"
    assert embed.description == ui.info_description
    assert [field.name for field in embed.fields] == [
        "🔎 Cómo consultar",
        "👁️ Visibilidad",
        "📋 Información disponible",
    ]
    assert "Solo para mí" in embed.fields[1].value


async def test_mdc_command_rejects_non_operational_member_without_querying(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def lookup_exact(_name: str) -> MdcRecord:
        raise AssertionError("unauthorized users must not reach the SARP API")

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = MdcCog(SimpleNamespace(cached=cached), SimpleNamespace(lookup_exact=lookup_exact), persona)
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await MdcCog.mdc.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == persona.messages.interface.mdc.unauthorized


async def test_mdc_command_opens_modal_for_operational_member(persona) -> None:
    modals: list[discord.ui.Modal] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def send_modal(modal: discord.ui.Modal) -> None:
        modals.append(modal)

    cog = MdcCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(guild=member.guild, author=member, send_modal=send_modal)

    await MdcCog.mdc.callback(cog, context)  # type: ignore[arg-type]

    assert isinstance(modals[0], MdcSearchModal)


async def test_mdc_info_is_public_for_operational_member(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = MdcCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await MdcCog.info_mdc.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert responses[0]["embed"].title == "🖥️ Cómo usar el MDC"


async def test_mdc_info_rejects_non_operational_member(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = MdcCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await MdcCog.info_mdc.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == persona.messages.interface.mdc.unauthorized


async def test_mdc_modal_search_publishes_public_interaction_response(persona) -> None:
    member = _member(11, (200,))
    followups: list[dict[str, object]] = []
    deferred: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def lookup_exact(_name: str) -> MdcRecord:
        return MdcRecord(name="Terry McKenzie", incarcerated=False)

    async def followup_send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=22),
        user=member,
        response=SimpleNamespace(defer=defer, is_done=lambda: True),
        followup=SimpleNamespace(send=followup_send),
    )
    cog = MdcCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(lookup_exact=lookup_exact),
        persona,
    )

    await cog.search(interaction, requester_id=11, raw_name="Terry McKenzie")

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert followups[0]["embed"].title == "🖥️ MDC · Terry McKenzie"
    assert followups[0]["allowed_mentions"].everyone is False
    assert followups[0]["allowed_mentions"].roles is False


async def test_mdc_modal_search_can_keep_result_private(persona) -> None:
    member = _member(11, (200,))
    followups: list[dict[str, object]] = []
    deferred: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def lookup_exact(_name: str) -> MdcRecord:
        return MdcRecord(name="Terry McKenzie", incarcerated=False)

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def followup_send(**kwargs: object) -> None:
        followups.append(kwargs)

    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=22),
        user=member,
        response=SimpleNamespace(defer=defer, is_done=lambda: True),
        followup=SimpleNamespace(send=followup_send),
    )
    cog = MdcCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(lookup_exact=lookup_exact),
        persona,
    )

    await cog.search(
        interaction,
        requester_id=11,
        raw_name="Terry McKenzie",
        visibility=MdcVisibility.PRIVATE,
    )

    assert deferred == [{"ephemeral": True}]
    assert followups[0]["embed"].title == "🖥️ MDC · Terry McKenzie"
    assert followups[0]["ephemeral"] is True
    assert followups[0]["allowed_mentions"].everyone is False


@pytest.mark.parametrize(
    ("visibility", "expected_ephemeral"),
    (
        (MdcVisibility.PUBLIC, False),
        (MdcVisibility.PRIVATE, True),
    ),
)
async def test_mdc_not_found_follows_selected_visibility(
    persona,
    visibility: MdcVisibility,
    expected_ephemeral: bool,
) -> None:
    member = _member(11, (200,))
    followups: list[dict[str, object]] = []
    deferred: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def lookup_exact(_name: str) -> MdcRecord:
        raise MdcCharacterNotFoundError

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def followup_send(**kwargs: object) -> None:
        followups.append(kwargs)

    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=22),
        user=member,
        response=SimpleNamespace(defer=defer, is_done=lambda: True),
        followup=SimpleNamespace(send=followup_send),
    )
    cog = MdcCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(lookup_exact=lookup_exact),
        persona,
    )

    await cog.search(
        interaction,
        requester_id=11,
        raw_name="Nombre Inexistente",
        visibility=visibility,
    )

    assert deferred == [{"ephemeral": expected_ephemeral}]
    assert followups[0]["ephemeral"] is expected_ephemeral
    assert followups[0]["embed"].title == persona.messages.interface.mdc.alert_title
    assert "Nombre Inexistente" in followups[0]["embed"].description


async def test_mdc_form_has_fixed_exact_name_copy(persona) -> None:
    modal = MdcSearchModal(MdcCog(SimpleNamespace(), SimpleNamespace(), persona), 11)

    assert modal.title == "Consultar MDC"
    assert modal.name.placeholder == "Ej.: Nombre Apellido"
    assert modal.name.min_length == 3
    assert modal.name.max_length == 100
    assert [option.label for option in modal.visibility.options] == [
        "Visible para todos",
        "Solo para mí",
    ]
    assert modal.visibility.options[0].default is True
