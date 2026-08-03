from __future__ import annotations

import pytest

from arrest_report_agent.config import AppSettings, ModelCatalog, ResolvedModel


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        DISCORD_TOKEN="discord-test-token",
        OPENAI_API_KEY="openai-test-key",
        DISCORD_GUILD_ID=1234674521416663070,
        REVIEW_CHANNEL_IDS="1462992794569281617,1462992859761217745",
        AUTHORIZED_USER_IDS="*",
        BOT_DISPLAY_NAME="Sgt. Vigil",
        BOT_ROLE_TITLE="Watch Sergeant",
        OPENAI_MODEL="gpt-5.6-terra",
        OPENAI_REASONING_EFFORT="medium",
        OPENAI_TRACING_ENABLED=False,
    )


@pytest.fixture
def model(settings: AppSettings) -> ResolvedModel:
    return ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
