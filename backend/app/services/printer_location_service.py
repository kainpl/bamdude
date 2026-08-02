"""Naming rules for a place, in one spot.

Deliberately the same shape as ``location_service`` (spool storage): one idea
for case-insensitive identity, learned once. Without a folded key, "Цех 2" and
"цех 2" are two places — which is the condition this entity exists to end.
"""

from __future__ import annotations


def normalize_location(name: str | None) -> str:
    """What gets stored as the name: the operator's capitalisation, trimmed."""
    return (name or "").strip()


def location_key(name: str | None) -> str:
    """The case-insensitive identity. Two names with this key are one place."""
    return normalize_location(name).lower()
