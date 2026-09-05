"""Naming rules for a tag, and the link writes, in one spot.

Same shape as ``printer_location_service``: one idea for case-insensitive
identity, learned once. The link table is written only here — SQLite ignores
ON DELETE, so every path that would rely on a cascade calls these instead.
"""

from __future__ import annotations

from sqlalchemy import delete

from backend.app.models.printer_tag import PrinterTagLink


def normalize_tag(name: str | None) -> str:
    """What gets stored as the name: the operator's capitalisation, trimmed."""
    return (name or "").strip()


def tag_key(name: str | None) -> str:
    """The case-insensitive identity. Two names with this key are one tag."""
    return normalize_tag(name).lower()


async def replace_links(db, printer_id: int, tag_ids: list[int]) -> None:
    """The printer's tag set becomes exactly ``tag_ids``. Flush, not commit — the route commits."""
    await db.execute(delete(PrinterTagLink).where(PrinterTagLink.printer_id == printer_id))
    for tag_id in sorted(set(tag_ids)):
        db.add(PrinterTagLink(printer_id=printer_id, tag_id=tag_id))
    await db.flush()


async def delete_links_for_printer(db, printer_id: int) -> None:
    await db.execute(delete(PrinterTagLink).where(PrinterTagLink.printer_id == printer_id))


async def delete_links_for_tag(db, tag_id: int) -> None:
    await db.execute(delete(PrinterTagLink).where(PrinterTagLink.tag_id == tag_id))
