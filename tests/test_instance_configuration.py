from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from arrest_report_agent.bootstrap.database import (
    DiscordBootstrapResolver,
    DiscordGuildInfo,
    plan_environment_import,
)
from arrest_report_agent.config import AppSettings, BrandingConfigurationError
from arrest_report_agent.config.bootstrap_profile import load_bootstrap_profile
from arrest_report_agent.config.instance import (
    DedeoOptions,
    KnowledgeLinksOptions,
    ReleaseRoutingOptions,
    SupportOptions,
)
from arrest_report_agent.dedeo.discord import DedeoCog, select_dedeo_target
from arrest_report_agent.infrastructure.discord.general_knowledge import (
    FAQ_ENTRIES,
    build_charge_search_embed,
    build_faq_embed,
    build_guide_embed,
)
from arrest_report_agent.knowledge import ReportGuide
from arrest_report_agent.release import ReleaseManifest
from arrest_report_agent.support import SupportCog, SupporterThanksView, append_support_note


def settings(**values):
    return AppSettings(_env_file=None, DISCORD_TOKEN="test", OPENAI_API_KEY="test", **values)


def test_repository_uses_instance_override_or_host_metadata_without_original_account() -> None:
    assert settings().resolved_repository_url is None
    automatic = settings(RAILWAY_GIT_REPO_OWNER="successor", RAILWAY_GIT_REPO_NAME="bot")
    assert automatic.resolved_repository_url == "https://github.com/successor/bot"
    explicit = settings(
        REPOSITORY_URL="https://github.com/faction/agent",
        RAILWAY_GIT_REPO_OWNER="successor",
        RAILWAY_GIT_REPO_NAME="bot",
    )
    assert explicit.resolved_repository_url == "https://github.com/faction/agent"
    manifest = ReleaseManifest.load_packaged(repository_url=explicit.resolved_repository_url)
    assert manifest.repository_full_name == "faction/agent"
    assert manifest.changelog_targets == "internal"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/team/bot",
        "https://github.com/team/bot?token=example",
        "https://user:pass@github.com/team/bot",
        "https://example.org/team/bot",
    ],
)
def test_repository_rejects_ambiguous_or_credential_bearing_urls(url) -> None:
    with pytest.raises(ValueError):
        settings(REPOSITORY_URL=url)


@pytest.mark.parametrize(
    "model, payload",
    [
        (SupportOptions, {"url": "http://example.org/support"}),
        (KnowledgeLinksOptions, {"procedures_manual_url": "https://user:pass@example.org/manual"}),
        (DedeoOptions, {"protected_user_ids": [1, 1]}),
        (DedeoOptions, {"protected_user_ids": [True]}),
        (ReleaseRoutingOptions, {"internal_channel_ids": [1, 1]}),
        (ReleaseRoutingOptions, {"internal_channel_ids": ["1"]}),
    ],
)
def test_instance_options_validate_urls_and_identifiers(model, payload) -> None:
    with pytest.raises(ValueError):
        model.model_validate(payload)


def test_no_implicit_creator_protection() -> None:
    previous_creator = SimpleNamespace(id=449044161954447360)
    requester = SimpleNamespace(id=101)
    result = select_dedeo_target((), author=requester, requested_target=previous_creator)
    assert result.target is previous_creator
    assert not result.creator_defense
    assert DedeoOptions().protected_user_ids == ()


@pytest.mark.asyncio
async def test_support_disabled_does_not_read_history_or_publish_a_link() -> None:
    repository = SimpleNamespace(ranked_supporters=AsyncMock())
    provider = SimpleNamespace(
        cached=AsyncMock(
            return_value=SimpleNamespace(
                settings=SimpleNamespace(support_options=None),
            )
        )
    )
    cog = SupportCog(repository, provider)
    ctx = SimpleNamespace(guild=SimpleNamespace(id=12), respond=AsyncMock())
    await SupportCog.support.callback(cog, ctx)
    await SupportCog.acknowledgements.callback(cog, ctx)
    repository.ranked_supporters.assert_not_awaited()
    assert all(call.kwargs["ephemeral"] for call in ctx.respond.await_args_list)
    assert all("http" not in str(call) for call in ctx.respond.await_args_list)
    embed = discord.Embed(description="Ayuda")
    assert append_support_note(embed).description == "Ayuda"
    view = SupporterThanksView(requester_id=1, rankings=())
    assert not any(isinstance(item, discord.ui.Button) and item.url for item in view.children)


@pytest.mark.asyncio
async def test_support_reads_each_guild_configuration_instead_of_the_original_url() -> None:
    options = {
        1: SupportOptions(url="https://buymeacoffee.com/operator-one"),
        2: SupportOptions(url="https://buymeacoffee.com/operator-two"),
    }

    async def cached(guild_id):
        return SimpleNamespace(settings=SimpleNamespace(support_options=options[guild_id]))

    cog = SupportCog(SimpleNamespace(), SimpleNamespace(cached=cached))
    for guild_id in options:
        ctx = SimpleNamespace(guild=SimpleNamespace(id=guild_id), respond=AsyncMock())
        await SupportCog.support.callback(cog, ctx)
        body = str(ctx.respond.await_args.kwargs["embed"].to_dict())
        assert str(options[guild_id].url) in body
        assert "southviking" not in body
    options[1] = None
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), respond=AsyncMock())
    await SupportCog.support.callback(cog, ctx)
    assert ctx.respond.await_args.kwargs["ephemeral"]


@pytest.mark.asyncio
async def test_disabled_dedeo_stops_before_member_selection(persona) -> None:
    member = MagicMock(spec=discord.Member)
    channel = MagicMock(spec=discord.TextChannel)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=11),
        author=member,
        channel=channel,
        respond=AsyncMock(),
    )
    provider = SimpleNamespace(
        cached=AsyncMock(
            return_value=SimpleNamespace(
                settings=SimpleNamespace(dedeo_options=None),
            )
        )
    )
    cog = DedeoCog(persona, provider)
    await DedeoCog.dede.callback(cog, ctx)
    assert ctx.respond.await_args.kwargs["ephemeral"]


def test_knowledge_links_are_optional_and_shared_by_all_navigation_builders(persona) -> None:
    ui = persona.messages.interface.knowledge
    links = KnowledgeLinksOptions(
        procedures_manual_url="https://example.org/our-manual",
        penal_code_url="https://example.org/our-code",
    )
    guide = ReportGuide.load_default()
    for configured in (links, None):
        embeds = (
            build_guide_embed(guide, section="overview", display_name="Bot", ui=ui, links=configured),
            build_faq_embed(FAQ_ENTRIES[0], display_name="Bot", ui=ui, links=configured),
            build_charge_search_embed((), query="prueba", display_name="Bot", ui=ui, links=configured),
        )
        output = json.dumps([e.to_dict() for e in embeds])
        assert "1DrhSgz" not in output
        assert "None" not in output
        assert ("https://example.org/our-manual" in output) is bool(configured)
        assert ("https://example.org/our-code" in output) is bool(configured)


def test_bootstrap_profile_can_be_replaced_without_inheriting_lssd(tmp_path) -> None:
    assert not load_bootstrap_profile("none").guilds
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "guilds": {
                    "99": {
                        "features": {
                            "teamspeak": {"enabled": True, "payload": {"address": "voice.example.org:9987"}}
                        },
                    }
                },
            }
        )
    )
    profile = load_bootstrap_profile(str(path))
    assert set(profile.guilds) == {99}
    assert set(profile.guilds[99].features) == {"teamspeak"}
    assert profile.guilds[99].features["teamspeak"].payload["address"] == "voice.example.org:9987"


def test_banner_accepts_local_png_and_explicitly_disables_it(tmp_path) -> None:
    from arrest_report_agent.config import load_global_profile_banner

    assert load_global_profile_banner("") is None
    assert load_global_profile_banner("none") is None
    packaged = load_global_profile_banner()
    assert packaged is not None
    path = tmp_path / "operator.png"
    path.write_bytes(packaged)
    assert load_global_profile_banner(str(path)) == packaged
    path.write_text("not an image")
    with pytest.raises(BrandingConfigurationError):
        load_global_profile_banner(str(path))


@pytest.mark.parametrize("profile_mode", ["none", "custom"])
async def test_import_respects_profile_selection_even_for_the_original_guild(
    tmp_path,
    monkeypatch,
    profile_mode,
) -> None:
    guild_id = 1234674521416663070
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "guilds": {
                    str(guild_id): {
                        "features": {
                            "support": {"enabled": True, "payload": {"url": "https://example.org/support"}},
                            "teamspeak": {"enabled": True, "payload": {"address": "voice.example.org:9987"}},
                        }
                    }
                },
            }
        )
    )
    configured = settings(
        BOT_PERSONA="walter_vigil",
        DISCORD_GUILD_IDS=str(guild_id),
        BOOTSTRAP_PROFILE_PATH="none" if profile_mode == "none" else str(path),
    )
    monkeypatch.setattr(
        DiscordBootstrapResolver,
        "guilds",
        AsyncMock(return_value=(DiscordGuildInfo(guild_id, "Guild"),)),
    )
    monkeypatch.setattr(DiscordBootstrapResolver, "role_owners", AsyncMock(return_value={}))
    plan = await plan_environment_import(configured)
    features = {feature.key: feature for feature in plan.guilds[0].features}
    assert all(not policy.ids for policy in plan.guilds[0].policies)
    if profile_mode == "none":
        assert not any(feature.enabled for feature in features.values())
    else:
        assert features["support"].payload["url"] == "https://example.org/support"
        assert features["teamspeak"].payload["address"] == "voice.example.org:9987"
        assert not features["dedeo"].enabled
