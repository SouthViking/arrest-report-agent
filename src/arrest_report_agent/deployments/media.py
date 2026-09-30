"""Original, unmodified illustration used only by release changelogs."""

from importlib.resources import files
from io import BytesIO

import discord

CHANGELOG_THUMBNAIL_FILENAME = "changelog-delivery.jpg"
CHANGELOG_THUMBNAIL_URL = f"attachment://{CHANGELOG_THUMBNAIL_FILENAME}"


def changelog_thumbnail_file() -> discord.File:
    resource = files("arrest_report_agent.deployments").joinpath("assets", CHANGELOG_THUMBNAIL_FILENAME)
    return discord.File(BytesIO(resource.read_bytes()), filename=CHANGELOG_THUMBNAIL_FILENAME)
