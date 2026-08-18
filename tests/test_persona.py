from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from arrest_report_agent.config import (
    AppSettings,
    PackagedPersonaProvider,
    PersonaConfigurationError,
    load_global_profile_banner,
)


@pytest.mark.asyncio
async def test_packaged_walter_persona_loads_with_avatar(loaded_persona) -> None:
    profile = loaded_persona.profile

    assert profile.id == "walter_vigil"
    assert profile.schema_version == 40
    assert profile.revision == 46
    assert profile.identity.full_name == "Walter Vigil"
    assert profile.discord.username == "Walter Vigil"
    assert profile.discord.nickname is None
    assert "Sargento de guardia" in profile.discord.bio
    role_info = profile.messages.interface.role_requests
    assert len(role_info.info_rules.splitlines()) == 2
    assert loaded_persona.avatar_bytes.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_packaged_roy_persona_loads_with_avatar() -> None:
    loaded = await PackagedPersonaProvider().load("roy_velazco")
    profile = loaded.profile

    assert profile.id == "roy_velazco"
    assert profile.schema_version == 40
    assert profile.revision == 53
    assert profile.identity.full_name == "Roy Velazco"
    assert profile.discord.username == "Roy Velazco"
    assert profile.discord.nickname is None
    assert profile.discord.avatar == "assets/roy-velazco-skin-avatar.png"
    assert "que el juez haga el resto" in profile.discord.bio
    assert any("chilenismos" in instruction for instruction in profile.personality.communication.conversation)
    assert any("veredicto" in instruction for instruction in profile.personality.communication.review_summary)
    assert any("hallazgos técnicos" in instruction for instruction in profile.personality.boundaries)
    assert len(profile.personality.examples.conversation) == 6
    assert any("saco wea" in example.assistant for example in profile.personality.examples.conversation)
    assert any(
        "q no entendiste" in example.assistant for example in profile.personality.examples.conversation
    )
    role_info = profile.messages.interface.role_requests
    assert len(role_info.info_rules.splitlines()) == 2
    assert {example.verdict for example in profile.personality.examples.review_summary} == {
        "approved",
        "corrections_required",
        "inconclusive",
    }
    assert loaded.avatar_bytes.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_personas_keep_personality_copy_with_capitalized_titles(loaded_persona) -> None:
    walter = loaded_persona.profile
    roy = (await PackagedPersonaProvider().load("roy_velazco")).profile

    assert walter.messages.access.supervisor_unauthorized != (roy.messages.access.supervisor_unauthorized)
    assert "Su rol" in walter.messages.access.supervisor_unauthorized
    assert "bonito intento" in roy.messages.access.supervisor_unauthorized
    assert "pa q arregle tu caga de acceso antes de volver" in roy.messages.access.unauthorized_user
    assert len(walter.messages.access.blocked_user_messages) == 3
    assert len(roy.messages.access.blocked_user_messages) == 3
    assert "lista de bloqueados" in roy.messages.access.blocked_user_messages[0]
    assert roy.messages.failures.review_failure_title == "⚠️ Se trabó el papeleo"
    assert walter.messages.interface.review.narrative_scope_label == "solo narrativa"
    assert roy.messages.interface.review.charges_scope_label == "los cargos"
    assert "Revisión parcial" in walter.messages.interface.review.partial_scope_title
    assert "Bien dentro" in roy.messages.interface.review.partial_approved_label
    assert "{focus}" in walter.messages.interface.review.targeted_focus
    assert "{display_name}" in walter.messages.help.description
    assert "{display_name}" in roy.messages.help.description
    assert "Muro de expedientes" in walter.messages.interface.supervision.shame_title
    assert "El muro de la vergüenza" in roy.messages.interface.supervision.shame_title
    assert roy.messages.interface.review.charge_review_title == "⚖️ Revisión de cargos"
    assert "📭" in walter.messages.interface.supervision.shame_legend
    assert "sin revisión" in roy.messages.interface.supervision.shame_legend
    assert "/info-reportes" in walter.messages.help.feature_guides
    assert "/info-reportes" in roy.messages.help.feature_guides
    assert "/pendientes" not in walter.messages.help.feature_guides
    assert "/ranking" not in roy.messages.help.feature_guides
    assert walter.messages.interface.supervision.honor_title == "🏆 Cuadro de honor"
    assert roy.messages.interface.supervision.honor_title == "🏆 Los que sí hicieron la pega"
    assert walter.messages.interface.role_requests.pending_title == "📋 Solicitud de roles"
    assert "supervisión" in roy.messages.interface.role_requests.pending_description.lower()
    assert "/info-roles" in walter.messages.help.feature_guides
    assert "/info-roles" in roy.messages.help.feature_guides
    assert walter.messages.interface.commands.info_reports_description
    assert roy.messages.interface.commands.info_reports_description
    assert "/reportes-pendientes" in walter.messages.help.quick_references
    assert "/reportes-pendientes" in roy.messages.help.quick_references
    assert walter.messages.interface.pending_arrest_reports.result_intro
    assert roy.messages.interface.pending_arrest_reports.result_intro
    assert "/info-pager" in walter.messages.help.feature_guides
    assert "/info-pager" in roy.messages.help.feature_guides
    assert "/info-callsigns" in walter.messages.help.feature_guides
    assert "/info-callsigns" in roy.messages.help.feature_guides
    assert "emisor" in walter.messages.interface.pagers.creator_cannot_respond.lower()
    assert "no te anotís" in roy.messages.interface.pagers.creator_cannot_respond
    assert "Llegaste tarde" in roy.messages.interface.pagers.stale
    assert "{callsign}" in walter.messages.interface.callsigns.saved_description
    assert "próxima vez" in roy.messages.interface.callsigns.saved_description
    assert walter.messages.interface.deployments.release_intro
    assert roy.messages.interface.deployments.crashed_description
    assert walter.messages.interface.deployments.ci_failed_description
    assert roy.messages.interface.deployments.ci_failed_description
    assert "/info-sugerencias" in walter.messages.help.feature_guides
    assert "/info-sugerencias" in roy.messages.help.feature_guides
    assert "sancionados" in walter.messages.interface.suggestions.info_warning
    assert "cualquier tontera" in roy.messages.interface.suggestions.info_warning
    assert "/pregunta-manual" in walter.messages.help.quick_references
    assert "/pregunta-manual" in roy.messages.help.quick_references
    assert len(walter.messages.interface.manual_quiz.correct_messages) == 3
    assert len(roy.messages.interface.manual_quiz.incorrect_messages) == 3
    assert len(walter.messages.interface.manual_quiz.expired_messages) == 3
    assert len(roy.messages.interface.manual_quiz.expired_messages) == 3
    assert "{seconds}" in walter.messages.interface.manual_quiz.cooldown
    assert walter.messages.interface.commands.manual_question_description
    assert roy.messages.interface.commands.manual_question_description
    assert "/info-mdc" in walter.messages.help.feature_guides
    assert "/info-mdc" in roy.messages.help.feature_guides
    assert "/mdc" not in walter.messages.help.quick_references
    assert "/mdc" not in roy.messages.help.quick_references
    assert walter.messages.interface.commands.mdc_description
    assert roy.messages.interface.commands.mdc_description
    assert walter.messages.interface.commands.info_mdc_description
    assert roy.messages.interface.commands.info_mdc_description
    assert "{name}" in walter.messages.interface.mdc.result_intro
    assert "{sections}" in roy.messages.interface.mdc.partial_warning
    assert "Solo para mí" in walter.messages.interface.mdc.info_visibility
    assert "Solo para mí" in roy.messages.interface.mdc.info_visibility


@pytest.mark.asyncio
async def test_packaged_provider_is_independent_from_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    loaded = await PackagedPersonaProvider().load("walter_vigil")

    assert loaded.profile.id == "walter_vigil"
    assert loaded.avatar_bytes


def test_global_profile_banner_is_packaged_png() -> None:
    banner = load_global_profile_banner()

    assert banner.startswith(b"\x89PNG")


@pytest.mark.parametrize("persona_id", ["../walter_vigil", "Walter_Vigil", "walter__vigil", ""])
@pytest.mark.asyncio
async def test_provider_rejects_unsafe_persona_ids(persona_id: str, tmp_path: Path) -> None:
    with pytest.raises(PersonaConfigurationError):
        await PackagedPersonaProvider(tmp_path).load(persona_id)


@pytest.mark.asyncio
async def test_provider_rejects_unknown_persona(tmp_path: Path) -> None:
    with pytest.raises(PersonaConfigurationError, match="unknown BOT_PERSONA"):
        await PackagedPersonaProvider(tmp_path).load("missing_persona")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.update({"id": "another_persona"}), "does not match BOT_PERSONA"),
        (lambda data: data.update({"unexpected": True}), "invalid persona"),
        (
            lambda data: data["discord"].update({"avatar": "../outside.png"}),
            "path must remain inside",
        ),
        (
            lambda data: data["discord"].update({"avatar": "assets/avatar.txt"}),
            "must be a PNG, JPEG or GIF",
        ),
        (
            lambda data: data["messages"]["access"].update(
                {"scope_refusal": "Texto con {placeholder_incorrecto}."}
            ),
            "invalid persona",
        ),
        (
            lambda data: data["messages"]["help"].update(
                {"footer": "Texto con {form_of_address.__class__}."}
            ),
            "invalid persona",
        ),
        (
            lambda data: data["messages"]["interface"]["knowledge"].update(
                {"charge_footer": "Página {page} sin los demás campos."}
            ),
            "invalid persona",
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_rejects_invalid_profiles_and_asset_paths(
    mutation,
    expected: str,
    tmp_path: Path,
    loaded_persona,
) -> None:
    data = loaded_persona.profile.model_dump(mode="json")
    mutation(data)
    (tmp_path / "walter_vigil.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PersonaConfigurationError, match=expected):
        await PackagedPersonaProvider(tmp_path).load("walter_vigil")


@pytest.mark.asyncio
async def test_provider_rejects_missing_and_spoofed_assets(
    tmp_path: Path,
    loaded_persona,
) -> None:
    data = loaded_persona.profile.model_dump(mode="json")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "walter-vigil-avatar.png").write_bytes(b"not a png")
    (tmp_path / "walter_vigil.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PersonaConfigurationError, match="does not match its image format"):
        await PackagedPersonaProvider(tmp_path).load("walter_vigil")

    (assets / "walter-vigil-avatar.png").unlink()
    with pytest.raises(PersonaConfigurationError, match="is unavailable"):
        await PackagedPersonaProvider(tmp_path).load("walter_vigil")


def test_persona_selection_rejects_invalid_or_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOT_PERSONA", raising=False)
    for value in ("", "../walter", "Walter Vigil"):
        with pytest.raises(ValidationError):
            AppSettings(
                _env_file=None,
                DISCORD_TOKEN="discord",
                OPENAI_API_KEY="openai",
                BOT_PERSONA=value,
            )


def test_loaded_persona_is_immutable(persona) -> None:
    with pytest.raises(ValidationError):
        persona.identity.display_name = "Changed"
