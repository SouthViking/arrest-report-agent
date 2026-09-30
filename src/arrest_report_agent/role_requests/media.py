"""Packaged decoration for role requests; upload the original bytes unchanged."""

from importlib.resources import files
from io import BytesIO

import discord

DELIVERY_THUMBNAIL_FILENAME = "role-delivery.jpg"
DELIVERY_THUMBNAIL_URL = f"attachment://{DELIVERY_THUMBNAIL_FILENAME}"


def delivery_thumbnail_attachment(message: discord.Message | None = None) -> dict[str, discord.File]:
    """Reuse an existing attachment on edits, including after a bot restart."""

    if message is not None and any(
        attachment.filename == DELIVERY_THUMBNAIL_FILENAME for attachment in message.attachments
    ):
        return {}
    resource = files("arrest_report_agent.role_requests").joinpath("assets", DELIVERY_THUMBNAIL_FILENAME)
    return {"file": discord.File(BytesIO(resource.read_bytes()), filename=DELIVERY_THUMBNAIL_FILENAME)}
