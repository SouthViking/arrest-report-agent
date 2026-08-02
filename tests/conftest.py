from __future__ import annotations

import pytest

from arrest_report_agent.config import AppSettings


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        DISCORD_TOKEN="discord-test-token",
        OPENAI_API_KEY="openai-test-key",
        DISCORD_GUILD_ID=1234674521416663070,
        REVIEW_CHANNEL_IDS="1462992794569281617,1462992859761217745",
        BOT_DISPLAY_NAME="Sgt. Vigil",
        BOT_ROLE_TITLE="Watch Sergeant",
        OPENAI_TRACING_ENABLED=False,
    )

