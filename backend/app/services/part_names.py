"""Canonical part names — folding slicer copy-suffixes into one part identity.

Rules validated against the whole farm (2026-08-29 survey, 1579 plates):

1. ``<anything>.<model-ext>`` + ``[ _-]`` + digits is ALWAYS a copy counter —
   the extension anchor makes it unambiguous (``X.stl_2``, ``X.stl 3``).
2. Trailing ``(N)`` is BambuStudio's clone suffix.
3. Extensionless ``base_N`` folds ONLY beside a sibling on the same plate
   (the bare ``base`` or ``base_M``, M != N) — a lone ``v2_bracket_3`` is a
   genuine name and stays itself.

Matching is case-insensitive (``name_key``); the original spelling is kept
for display.
"""

import re
from dataclasses import dataclass, field

_EXT = r"(?:stl|stp|step|3mf|obj|ply|amf)"
_RX_EXT_COPY = re.compile(rf"^(.+\.{_EXT})[ _\-]\d+$", re.IGNORECASE)
_RX_PAREN = re.compile(r"^(.+?)\s*\(\d+\)$")
_RX_TAIL_N = re.compile(r"^(.+?)[ _\-](\d+)$")


def canonicalize(name: str, plate_names: list[str] | None = None) -> str:
    n = " ".join((name or "").split())
    m = _RX_EXT_COPY.match(n)
    if m:
        return m.group(1)
    m = _RX_PAREN.match(n)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _RX_TAIL_N.match(n)
    if m and plate_names:
        base, idx = m.group(1), m.group(2)
        for other in plate_names:
            o = " ".join((other or "").split())
            if o == n:
                continue
            if o == base:
                return base
            om = _RX_TAIL_N.match(o)
            if om and om.group(1) == base and om.group(2) != idx:
                return base
    return n


def name_key(canonical: str) -> str:
    return canonical.lower()


@dataclass
class PartTally:
    """One canonical part on one plate: its instances and their ids."""

    name: str
    name_key: str
    identify_ids: list[int] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.identify_ids)


def tally_objects(objects: dict[int, str]) -> list[PartTally]:
    """Group ``extract_printable_objects_from_3mf`` output (id -> raw name)
    into canonical parts. Sibling folding sees the whole plate's names."""
    names = list(objects.values())
    grouped: dict[str, PartTally] = {}
    for obj_id, raw in objects.items():
        canon = canonicalize(raw, names)
        key = name_key(canon)
        row = grouped.get(key)
        if row is None:
            row = grouped[key] = PartTally(name=canon, name_key=key)
        row.identify_ids.append(obj_id)
    return list(grouped.values())
