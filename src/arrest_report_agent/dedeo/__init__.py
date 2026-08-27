"""Deterministic, stateless Discord dedeo command."""

from .discord import DedeoCog, build_dedeo_embed, eligible_channel_members, select_dedeo_target

__all__ = [
    "DedeoCog",
    "build_dedeo_embed",
    "eligible_channel_members",
    "select_dedeo_target",
]
