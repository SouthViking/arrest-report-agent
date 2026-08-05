from __future__ import annotations

import asyncio

import pytest

from arrest_report_agent.config import (
    AppSettings,
    BotPersona,
    LoadedPersona,
    ModelCatalog,
    PackagedPersonaProvider,
    ResolvedModel,
    load_global_profile_banner,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-ai-evals",
        action="store_true",
        default=False,
        help="run opt-in evaluations against the configured OpenAI model",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-ai-evals"):
        return

    skip_ai_eval = pytest.mark.skip(
        reason="live AI evaluation; pass --run-ai-evals to allow OpenAI usage",
    )
    for item in items:
        if "ai_eval" in item.keywords:
            item.add_marker(skip_ai_eval)


@pytest.fixture(autouse=True)
def configure_persona_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_PERSONA", "walter_vigil")


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord-test-token",
        OPENAI_API_KEY="openai-test-key",
        DISCORD_GUILD_IDS="1234674521416663070",
        REVIEW_CHANNEL_IDS="1462992794569281617,1462992859761217745",
        AUTHORIZED_USER_IDS="*",
        BOT_PERSONA="walter_vigil",
        OPENAI_MODEL="gpt-5.6-terra",
        OPENAI_REASONING_EFFORT="medium",
        OPENAI_TRACING_ENABLED=False,
    )


@pytest.fixture(scope="session")
def loaded_persona() -> LoadedPersona:
    return asyncio.run(PackagedPersonaProvider().load("walter_vigil"))


@pytest.fixture(scope="session")
def global_banner_bytes() -> bytes:
    return load_global_profile_banner()


@pytest.fixture(scope="session")
def persona(loaded_persona: LoadedPersona) -> BotPersona:
    return loaded_persona.profile


@pytest.fixture
def model(settings: AppSettings) -> ResolvedModel:
    return ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
