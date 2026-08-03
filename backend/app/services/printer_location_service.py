"""Naming rules for a place, in one spot.

Deliberately the same shape as ``location_service`` (spool storage): one idea
for case-insensitive identity, learned once. Without a folded key, "Цех 2" and
"цех 2" are two places — which is the condition this entity exists to end.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select


def normalize_location(name: str | None) -> str:
    """What gets stored as the name: the operator's capitalisation, trimmed."""
    return (name or "").strip()


def location_key(name: str | None) -> str:
    """The case-insensitive identity. Two names with this key are one place."""
    return normalize_location(name).lower()


PATH_SEPARATOR = " / "
# Deep enough for a workshop, a shelf and a box. Not a technical limit: the path
# is a label in a group header, and "workshop / shelf 1 / box / section / corner"
# stops being one.
MAX_DEPTH = 3


@dataclass(frozen=True)
class LocationNode:
    id: int
    name: str
    parent_id: int | None


async def load_tree(db) -> dict[int, LocationNode]:
    """Every location, by id.

    Loaded whole rather than asked with a recursive CTE: this table holds tens
    of rows on the largest farm, so one small SELECT is cheaper than the
    recursion and far easier to test.
    """
    from backend.app.models.printer_location import PrinterLocation

    rows = (await db.execute(select(PrinterLocation.id, PrinterLocation.name, PrinterLocation.parent_id))).all()
    return {row.id: LocationNode(id=row.id, name=row.name, parent_id=row.parent_id) for row in rows}


def _ancestry(tree: dict[int, LocationNode], location_id: int) -> list[LocationNode]:
    """From the row itself up to its root, stopping on anything broken.

    The visited set is not paranoia: a cycle here would spin for ever, and this
    runs inside a request.
    """
    chain: list[LocationNode] = []
    seen: set[int] = set()
    current = tree.get(location_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = tree.get(current.parent_id) if current.parent_id is not None else None
    return chain


def subtree_ids(tree: dict[int, LocationNode], root_id: int) -> set[int]:
    """The root and everything beneath it.

    Empty for an id that is not there — a location deleted between two requests
    must not fail the queue.
    """
    if root_id not in tree:
        return set()
    found = {root_id}
    growing = True
    while growing:
        growing = False
        for node in tree.values():
            if node.parent_id in found and node.id not in found:
                found.add(node.id)
                growing = True
    return found


def path_of(tree: dict[int, LocationNode], location_id: int) -> str:
    """ "Workshop / Shelf 1 / Box", read from the root down."""
    return PATH_SEPARATOR.join(node.name for node in reversed(_ancestry(tree, location_id)))


def depth_of(tree: dict[int, LocationNode], location_id: int) -> int:
    """A root is 1."""
    return len(_ancestry(tree, location_id))


def would_cycle(tree: dict[int, LocationNode], location_id: int, new_parent_id: int | None) -> bool:
    """Whether giving this location that parent would make a ring."""
    if new_parent_id is None:
        return False
    if new_parent_id == location_id:
        return True
    return any(node.id == location_id for node in _ancestry(tree, new_parent_id))
