"""Printer tags: list, create, rename, delete.

No new permissions — a label list is farm configuration, so reads ride
``PRINTERS_READ`` and writes ride ``PRINTERS_UPDATE``, exactly like locations.

A tag deletes freely and takes its links with it — in code, because SQLite
ignores ON DELETE. (The stagger-groups task adds the one refusal: a tag that
Settings uses as a staggered-start group.)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_tag import PrinterTag, PrinterTagLink
from backend.app.models.user import User
from backend.app.schemas.printer_tag import PrinterTagCreate, PrinterTagListItem, PrinterTagOut, PrinterTagUpdate
from backend.app.services.printer_tag_service import delete_links_for_tag, normalize_tag, tag_key

router = APIRouter(prefix="/printer-tags", tags=["printer-tags"])

_NAME_TAKEN = "A tag with this name already exists."


async def _name_is_taken(db, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitively. The unique index is the backstop; this is the guard that speaks."""
    query = select(PrinterTag.id).where(PrinterTag.name_key == tag_key(name))
    if exclude_id is not None:
        query = query.where(PrinterTag.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


async def _printer_counts(db) -> dict[int, int]:
    """Live printers per tag. Archived ones are hidden everywhere else, so not counted here."""
    rows = (
        await db.execute(
            select(PrinterTagLink.tag_id, func.count())
            .join(Printer, Printer.id == PrinterTagLink.printer_id)
            .where(Printer.archived.is_(False))
            .group_by(PrinterTagLink.tag_id)
        )
    ).all()
    return dict(rows)


@router.get("")
async def list_tags(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
):
    counts = await _printer_counts(db)
    rows = (await db.execute(select(PrinterTag).order_by(PrinterTag.name_key))).scalars().all()
    return {
        "tags": [
            PrinterTagListItem(id=row.id, name=row.name, printer_count=counts.get(row.id, 0), is_stagger_group=False)
            for row in rows
        ]
    }


@router.post("", status_code=201, response_model=PrinterTagOut)
async def create_tag(
    payload: PrinterTagCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    name = normalize_tag(payload.name)
    if await _name_is_taken(db, name):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    row = PrinterTag(name=name, name_key=tag_key(name))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return PrinterTagOut.model_validate(row)


@router.patch("/{tag_id}", response_model=PrinterTagOut)
async def rename_tag(
    tag_id: int,
    payload: PrinterTagUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    row = await db.get(PrinterTag, tag_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such tag.")
    name = normalize_tag(payload.name)
    if await _name_is_taken(db, name, exclude_id=tag_id):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    row.name = name
    # Rewritten with the name, always — see rename_location for the duplicate
    # that slips through when the key keeps the old spelling.
    row.name_key = tag_key(name)
    await db.commit()
    await db.refresh(row)
    return PrinterTagOut.model_validate(row)


@router.delete("/{tag_id}")
async def delete_tag(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    row = await db.get(PrinterTag, tag_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such tag.")
    await delete_links_for_tag(db, tag_id)
    await db.delete(row)
    await db.commit()
    return {"deleted": tag_id}
