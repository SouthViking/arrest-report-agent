"""Voluntary support command and reusable Discord presentation helpers."""

from .discord import (
    SupportCog,
    SupporterThanksView,
    append_support_note,
    build_acknowledgements_embed,
    build_support_embed,
)

__all__ = [
    "SupportCog",
    "SupporterThanksView",
    "append_support_note",
    "build_acknowledgements_embed",
    "build_support_embed",
]
