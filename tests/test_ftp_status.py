from __future__ import annotations

from types import SimpleNamespace

import discord

from arrest_report_agent.application.member_names import (
    PersonNameMatchStrength,
    normalize_person_name,
    person_name_aliases,
    person_name_match_strength,
)
from arrest_report_agent.application.ports import (
    GuildMemberProfile,
    GuildMemberSearchResult,
)
from arrest_report_agent.config import FtpStatusOptions
from arrest_report_agent.faction_directory import (
    FactionDirectoryMember,
    FactionDirectorySnapshot,
)
from arrest_report_agent.ftp_status import (
    FtpMemberGroup,
    FtpStatusCog,
    FtpStatusService,
    build_ftp_status_embed,
)


def _options() -> FtpStatusOptions:
    return FtpStatusOptions(
        faction_id=3,
        supervisory_role_ids=(10,),
        mfto_role_ids=(20,),
        fto_role_ids=(30,),
        ftt_role_ids=(40,),
    )


def _faction_member(name: str, *, online: bool = True, on_duty: bool = False):
    return FactionDirectoryMember(
        name=name,
        online=online,
        on_duty=on_duty,
        rank_name="Deputy Sheriff",
        rank_order=1,
        salary=None,
        last_connected_at=None,
        faction_time_minutes=0,
    )


def _discord_member(
    user_id: int,
    display_name: str,
    role_ids: tuple[int, ...],
) -> GuildMemberProfile:
    return GuildMemberProfile(
        user_id=user_id,
        display_name=display_name,
        username=f"user-{user_id}",
        roles=(),
        joined_at=None,
        created_at=discord.utils.utcnow(),
        is_bot=False,
        role_ids=role_ids,
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


def test_person_name_matching_normalizes_pcu_names_aliases_and_accents() -> None:
    aliases = person_name_aliases("J. Gómez / E. Mendoza")

    assert normalize_person_name("José_Gómez") == "jose gomez"
    assert aliases == ("j gomez", "e mendoza")
    assert person_name_match_strength("José_Gómez", aliases) == (PersonNameMatchStrength.INITIAL_AND_SURNAME)
    assert person_name_match_strength("Elias_Mendoza", aliases) == (
        PersonNameMatchStrength.INITIAL_AND_SURNAME
    )


async def test_ftp_status_joins_unique_aliases_and_prioritizes_highest_role() -> None:
    roster = FactionDirectorySnapshot(
        faction_id=3,
        members=(
            _faction_member("José Gómez", on_duty=True),
            _faction_member("Derek Valdez"),
            _faction_member("Ana Muñoz", on_duty=True),
            _faction_member("Offline Person", online=False),
        ),
    )
    candidates = (
        _discord_member(1, "J. Gomez / E. Mendoza", (10, 20)),
        _discord_member(2, "D. Valdez / A. Freeman", (30,)),
        _discord_member(3, "Ana Munoz", (40,)),
    )
    searches: list[object] = []

    async def snapshot(faction_id: int) -> FactionDirectorySnapshot:
        assert faction_id == 3
        return roster

    async def search_members(guild_id: int, query: object) -> GuildMemberSearchResult:
        assert guild_id == 99
        searches.append(query)
        return GuildMemberSearchResult(members=candidates)

    service = FtpStatusService(
        SimpleNamespace(snapshot=snapshot),
        SimpleNamespace(search_members=search_members),
    )
    result = await service.query(99, _options())

    assert [member.group for member in result.members] == [
        FtpMemberGroup.SUPERVISORY,
        FtpMemberGroup.FTO,
        FtpMemberGroup.FTT,
    ]
    assert [member.user_id for member in result.members] == [1, 2, 3]
    assert result.on_duty_count == 2
    assert result.ambiguous_connected_characters == 0
    assert searches[0].role_ids_any == (10, 20, 30, 40)


async def test_ftp_status_omits_ambiguous_initial_and_surname_matches() -> None:
    roster = FactionDirectorySnapshot(
        faction_id=3,
        members=(_faction_member("Derek Valdez"),),
    )
    candidates = (
        _discord_member(1, "D. Valdez / E. Mendoza", (30,)),
        _discord_member(2, "D. Valdez / J. Soto", (30,)),
    )

    async def search_members(_guild_id: int, _query: object) -> GuildMemberSearchResult:
        return GuildMemberSearchResult(members=candidates)

    service = FtpStatusService(
        SimpleNamespace(snapshot=lambda _faction_id: _async_value(roster)),
        SimpleNamespace(search_members=search_members),
    )
    result = await service.query(99, _options())

    assert result.members == ()
    assert result.ambiguous_connected_characters == 1


async def _async_value(value: object) -> object:
    return value


async def test_ftp_command_is_public_and_limited_to_operational_staff(persona) -> None:
    member = _member(11, (200,))
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                ftp_status_options=_options(),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def query(_guild_id: int, _options: FtpStatusOptions):
        return SimpleNamespace(
            faction_id=3,
            members=(),
            on_duty_count=0,
            ambiguous_connected_characters=0,
            members_for=lambda _group: (),
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    cog = FtpStatusCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        defer=defer,
        followup=SimpleNamespace(send=send),
    )

    await FtpStatusCog.ftp_connected.callback(cog, context)  # type: ignore[arg-type]

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert followups[0]["allowed_mentions"].everyone is False


def test_ftp_embed_uses_separate_non_inline_fields_and_connected_scope() -> None:
    snapshot = SimpleNamespace(
        faction_id=3,
        members=(
            SimpleNamespace(
                user_id=1,
                display_name="Supervisor",
                group=FtpMemberGroup.SUPERVISORY,
                on_duty=True,
            ),
            SimpleNamespace(
                user_id=2,
                display_name="Trainee",
                group=FtpMemberGroup.FTT,
                on_duty=False,
            ),
        ),
        on_duty_count=1,
        members_for=lambda group: tuple(member for member in snapshot.members if member.group is group),
    )

    embed = build_ftp_status_embed(snapshot)

    assert embed.title == "🎓 FTP · Miembros conectados"
    assert "**Supervisores:** 1" in embed.description
    assert "**FTT:** 1" in embed.description
    assert embed.fields[0].name == "🧑‍💼 Supervisory Staff · 1"
    assert embed.fields[0].inline is False
    assert embed.fields[1].name == "🧑‍🎓 Field Training Trainees (FTT) · 1"
    assert embed.fields[1].inline is False
    assert embed.footer.text == "Solo se muestran miembros conectados en este momento."
