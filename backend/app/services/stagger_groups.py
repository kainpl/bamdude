"""Which stagger group(s) a printer heats in.

Staggered start caps how many printers heat at once. On a farm fed from
several electrical phases the cap is per phase, and the operator describes the
phases with printer tags, printer locations, or both (design:
docs/superpowers/specs/2026-09-05-stagger-groups-design.md). This module turns
that description into one question the scheduler asks in memory:
``groups_for(printer_id)``.

Rules (spec decisions 3, 5, 6):
- A group key is ``(tag_id, location_id)``. An axis that is off, or on with
  nothing picked, contributes ``None``. Both off → the single key ``GLOBAL``,
  today's one bucket, byte for byte.
- A printer with none of the picked tags is a WILDCARD on that axis: it
  belongs to every picked tag's group. Its phase is unknown, so it may be on
  any. Likewise a printer with no picked ancestor on the location axis.
- On the location axis a printer belongs to the nearest picked ancestor of its
  location, the location itself included.
- Two axes on → the Cartesian product.

Loaded once per scheduler tick — zero queries while no axis is active — and
never stored on a slot: a tag pinned while a printer heats counts on the next
tick.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from itertools import product

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

GroupKey = tuple[int | None, int | None]
GLOBAL: GroupKey = (None, None)

SETTING_BY_TAGS = "stagger_split_by_tags"
SETTING_TAG_IDS = "stagger_group_tag_ids"
SETTING_BY_LOCATION = "stagger_split_by_location"
SETTING_LOCATION_IDS = "stagger_group_location_ids"


def parse_id_list(raw: str | None) -> frozenset[int]:
    """The JSON array a settings row holds, or nothing. Malformed → nothing, said in the log."""
    if not raw or raw == "None":
        return frozenset()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Stagger group id list is not JSON, ignoring: %r", raw)
        return frozenset()
    if not isinstance(parsed, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in parsed):
        logger.warning("Stagger group id list is not a list of integers, ignoring: %r", raw)
        return frozenset()
    return frozenset(parsed)


@dataclass(frozen=True)
class StaggerSplit:
    """The four settings, parsed. ``active`` is what the scheduler branches on."""

    by_tags: bool = False
    tag_ids: frozenset[int] = frozenset()
    by_location: bool = False
    location_ids: frozenset[int] = frozenset()

    @property
    def tags_active(self) -> bool:
        return self.by_tags and bool(self.tag_ids)

    @property
    def location_active(self) -> bool:
        return self.by_location and bool(self.location_ids)

    @property
    def active(self) -> bool:
        return self.tags_active or self.location_active

    @classmethod
    async def from_settings(cls, db: AsyncSession) -> StaggerSplit:
        from backend.app.api.routes.settings import get_setting

        def _bool(value: str | None) -> bool:
            return (value or "false").lower() == "true"

        return cls(
            by_tags=_bool(await get_setting(db, SETTING_BY_TAGS)),
            tag_ids=parse_id_list(await get_setting(db, SETTING_TAG_IDS)),
            by_location=_bool(await get_setting(db, SETTING_BY_LOCATION)),
            location_ids=parse_id_list(await get_setting(db, SETTING_LOCATION_IDS)),
        )


class StaggerGroupResolver:
    """Built once from a session; every question after that is answered in memory."""

    def __init__(
        self,
        split: StaggerSplit,
        *,
        tags_by_printer: dict[int, frozenset[int]],
        tag_names: dict[int, str],
        location_by_printer: dict[int, int | None],
        parent_by_location: dict[int, int | None],
        location_names: dict[int, str],
    ) -> None:
        self.split = split
        # Only ids that still exist: a deleted tag left in Settings is nobody's group.
        self._tag_ids: frozenset[int] = (
            frozenset(t for t in split.tag_ids if t in tag_names) if split.tags_active else frozenset()
        )
        self._location_ids: frozenset[int] = (
            frozenset(loc for loc in split.location_ids if loc in location_names)
            if split.location_active
            else frozenset()
        )
        self._tags_by_printer = tags_by_printer
        self._tag_names = tag_names
        self._location_by_printer = location_by_printer
        self._parent_by_location = parent_by_location
        self._location_names = location_names

    @classmethod
    def global_only(cls) -> StaggerGroupResolver:
        """One bucket for everyone — what an inactive split, or a disabled stagger, means."""
        return cls(
            StaggerSplit(),
            tags_by_printer={},
            tag_names={},
            location_by_printer={},
            parent_by_location={},
            location_names={},
        )

    @classmethod
    async def load(cls, db: AsyncSession, split: StaggerSplit) -> StaggerGroupResolver:
        if not split.active:
            return cls.global_only()

        tags_by_printer: dict[int, set[int]] = {}
        tag_names: dict[int, str] = {}
        location_by_printer: dict[int, int | None] = {}
        parent_by_location: dict[int, int | None] = {}
        location_names: dict[int, str] = {}

        if split.tags_active:
            from backend.app.models.printer_tag import PrinterTag, PrinterTagLink

            for tag_id, name in (
                await db.execute(select(PrinterTag.id, PrinterTag.name).where(PrinterTag.id.in_(split.tag_ids)))
            ).all():
                tag_names[tag_id] = name
            for printer_id, tag_id in (
                await db.execute(
                    select(PrinterTagLink.printer_id, PrinterTagLink.tag_id).where(
                        PrinterTagLink.tag_id.in_(split.tag_ids)
                    )
                )
            ).all():
                tags_by_printer.setdefault(printer_id, set()).add(tag_id)

        if split.location_active:
            from backend.app.models.printer import Printer
            from backend.app.models.printer_location import PrinterLocation

            for loc_id, parent_id, name in (
                await db.execute(select(PrinterLocation.id, PrinterLocation.parent_id, PrinterLocation.name))
            ).all():
                parent_by_location[loc_id] = parent_id
                location_names[loc_id] = name
            for printer_id, location_id in (await db.execute(select(Printer.id, Printer.location_id))).all():
                location_by_printer[printer_id] = location_id

        return cls(
            split,
            tags_by_printer={k: frozenset(v) for k, v in tags_by_printer.items()},
            tag_names=tag_names,
            location_by_printer=location_by_printer,
            parent_by_location=parent_by_location,
            location_names=location_names,
        )

    # ── what is in effect ─────────────────────────────────────────────────

    @property
    def tags_split(self) -> bool:
        return bool(self._tag_ids)

    @property
    def location_split(self) -> bool:
        return bool(self._location_ids)

    # ── axis values ───────────────────────────────────────────────────────

    def _own_tags(self, printer_id: int) -> frozenset[int]:
        return self._tags_by_printer.get(printer_id, frozenset()) & self._tag_ids

    def _tag_values(self, printer_id: int) -> frozenset[int | None]:
        if not self._tag_ids:
            return frozenset({None})
        return self._own_tags(printer_id) or self._tag_ids  # wildcard: every picked tag

    def _nearest_picked_location(self, printer_id: int) -> int | None:
        """Up the parent chain from the printer's own place. The visited set is not
        paranoia: a ring here would spin inside the scheduler tick."""
        current = self._location_by_printer.get(printer_id)
        seen: set[int] = set()
        while current is not None and current not in seen:
            if current in self._location_ids:
                return current
            seen.add(current)
            current = self._parent_by_location.get(current)
        return None

    def _location_values(self, printer_id: int) -> frozenset[int | None]:
        if not self._location_ids:
            return frozenset({None})
        nearest = self._nearest_picked_location(printer_id)
        return frozenset({nearest}) if nearest is not None else self._location_ids  # wildcard

    # ── questions ─────────────────────────────────────────────────────────

    def groups_for(self, printer_id: int) -> frozenset[GroupKey]:
        return frozenset(product(self._tag_values(printer_id), self._location_values(printer_id)))

    def is_wildcard(self, printer_id: int) -> bool:
        tag_wild = bool(self._tag_ids) and not self._own_tags(printer_id)
        location_wild = bool(self._location_ids) and self._nearest_picked_location(printer_id) is None
        return tag_wild or location_wild

    @property
    def universe(self) -> frozenset[GroupKey]:
        tags: frozenset[int | None] = self._tag_ids or frozenset({None})
        locations: frozenset[int | None] = self._location_ids or frozenset({None})
        return frozenset(product(tags, locations))

    def label(self, key: GroupKey) -> str | None:
        """The group's name: "Фаза 1 · Цех 2". None for the global group."""
        tag_id, location_id = key
        parts = [
            self._tag_names.get(tag_id) if tag_id is not None else None,
            self._location_names.get(location_id) if location_id is not None else None,
        ]
        present = [p for p in parts if p]
        return " · ".join(present) if present else None
