"""Pluggable venue registry.

`fetch(conf, year)` returns a list of candidate dicts
{title, authors, track, venue, year}. New venues register an adapter here.
"""
from __future__ import annotations

from . import ai, systems


def supported() -> list[str]:
    return list(ai.AI_VENUES.keys()) + list(systems.CONF_CONFIG.keys())


def fetch(conf: str, year: int) -> list[dict]:
    """Fetch accepted-paper candidates for `conf` in `year`."""
    conf = conf.upper()
    if conf in ai.AI_VENUES:
        return ai.fetch(conf, year)
    if conf in systems.CONF_CONFIG:
        return systems.fetch(conf, year)
    raise KeyError(
        f"venue '{conf}' has no adapter; supported: {', '.join(supported())}"
    )
