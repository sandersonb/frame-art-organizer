"""Matte helpers — pure functions, no TV or DB access.

The Frame's matte is a string: ``none`` (fill the panel) or ``{type}_{color}``, e.g.
``shadowbox_polar``. The TV is the matte editor; we read its choice back (the "harvest",
SPEC.md §12.4) and compare it to what we last uploaded, so comparisons must be insensitive to
the ways the firmware spells "no matte".
"""
from __future__ import annotations


def normalize(value) -> str:
    """Canonical form of a matte id for comparison.

    ``None``, ``''``, whitespace and ``'none'`` (any case) all mean "no matte" → ``'none'``.
    Anything else is stripped and lower-cased (the TV's ids are lower-case already).
    """
    if value is None:
        return "none"
    text = str(value).strip().lower()
    return text or "none"
