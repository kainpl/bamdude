"""Designing a label: templates, sheets, the vocabulary, and a live preview.

Printing is not here — that stays on ``/inventory/labels`` and ``/spoolman/labels``,
which now take a ``template_id`` as well as the six names they always took.
This router only answers what a design *is*.

⚠️ **A built-in cannot be edited or deleted.** Its ``builtin_key`` is a name the
label API accepts, so an automation that has printed the same label for a year
must not quietly start printing a different one because somebody dragged a box.
Duplicating gives an editable copy, which is what "edit a built-in" actually
means.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.schemas.label_template import (
    LabelPreviewRequest,
    LabelSheetOut,
    LabelTemplateIn,
    LabelTemplateOut,
)
from backend.app.services.label_context import example_context, spool_context
from backend.app.services.label_raster import render_template_png
from backend.app.services.label_template import PLACEHOLDERS, LabelTemplateSpec, Placeholder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/label-templates", tags=["label-templates"])


def _to_out(row: LabelTemplate) -> LabelTemplateOut:
    return LabelTemplateOut(
        id=row.id,
        name=row.name,
        width_mm=row.width_mm,
        height_mm=row.height_mm,
        shape=row.shape,
        elements=row.elements or [],
        builtin_key=row.builtin_key,
        is_builtin=row.is_builtin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_spec(body: LabelTemplateIn) -> LabelTemplateSpec:
    """Validate a posted design as the renderers will see it.

    The two models carry the same fields on purpose — the API one exists to be
    a request body, the spec one is what every renderer takes — and this is the
    single place they meet.
    """
    try:
        return LabelTemplateSpec(**body.model_dump())
    except ValidationError as error:
        raise HTTPException(422, f"Invalid template: {error}") from error


async def _load(db: AsyncSession, template_id: int) -> LabelTemplate:
    row = await db.get(LabelTemplate, template_id)
    if row is None:
        raise HTTPException(404, f"Label template {template_id} not found")
    return row


@router.get("", response_model=list[LabelTemplateOut])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> list[LabelTemplateOut]:
    """Every design, built-ins first — they are what a fresh install prints."""
    result = await db.execute(select(LabelTemplate).order_by(LabelTemplate.builtin_key.is_(None), LabelTemplate.name))
    return [_to_out(row) for row in result.scalars().all()]


@router.get("/placeholders", response_model=list[Placeholder])
async def list_placeholders(
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> list[Placeholder]:
    """The vocabulary a template's text may use, with an example for each.

    Served rather than duplicated in the frontend: the editor's field picker and
    the renderer have to agree about what ``{remaining_g}`` means, and a second
    hand-maintained list is how they stop agreeing.
    """
    return list(PLACEHOLDERS)


@router.get("/sheets", response_model=list[LabelSheetOut])
async def list_sheets(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> list[LabelSheetOut]:
    """Paper geometries. Read-only for now — a sheet editor is its own feature."""
    result = await db.execute(select(LabelSheet).order_by(LabelSheet.name))
    return [
        LabelSheetOut(
            id=row.id,
            name=row.name,
            builtin_key=row.builtin_key,
            page_size=row.page_size,
            cell_width_mm=row.cell_width_mm,
            cell_height_mm=row.cell_height_mm,
            cols=row.cols,
            rows=row.rows,
            margin_top_mm=row.margin_top_mm,
            margin_left_mm=row.margin_left_mm,
            gap_x_mm=row.gap_x_mm,
            gap_y_mm=row.gap_y_mm,
        )
        for row in result.scalars().all()
    ]


@router.get("/{template_id}", response_model=LabelTemplateOut)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> LabelTemplateOut:
    return _to_out(await _load(db, template_id))


@router.post("", response_model=LabelTemplateOut, status_code=201)
async def create_template(
    body: LabelTemplateIn,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelTemplateOut:
    _as_spec(body)
    row = LabelTemplate(
        name=body.name,
        width_mm=body.width_mm,
        height_mm=body.height_mm,
        shape=body.shape,
        elements=[e.model_dump() for e in body.elements],
        builtin_key=None,
        created_by=user.id if user else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


@router.put("/{template_id}", response_model=LabelTemplateOut)
async def update_template(
    template_id: int,
    body: LabelTemplateIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelTemplateOut:
    row = await _load(db, template_id)
    if row.is_builtin:
        raise HTTPException(
            409,
            f"'{row.name}' is a built-in design and cannot be edited — duplicate it to make a copy you own",
        )

    _as_spec(body)
    row.name = body.name
    row.width_mm = body.width_mm
    row.height_mm = body.height_mm
    row.shape = body.shape
    row.elements = [e.model_dump() for e in body.elements]
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


@router.post("/{template_id}/duplicate", response_model=LabelTemplateOut, status_code=201)
async def duplicate_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelTemplateOut:
    """A copy somebody owns. ⚠️ The key is deliberately not carried over — two
    rows answering to ``box_40x30`` would make which label prints a coin toss.
    """
    source = await _load(db, template_id)
    copy = LabelTemplate(
        name=f"{source.name} (copy)"[:120],
        width_mm=source.width_mm,
        height_mm=source.height_mm,
        shape=source.shape,
        elements=list(source.elements or []),
        builtin_key=None,
        created_by=user.id if user else None,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return _to_out(copy)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> None:
    row = await _load(db, template_id)
    if row.is_builtin:
        raise HTTPException(409, f"'{row.name}' is a built-in design and cannot be deleted")
    await db.delete(row)
    await db.commit()


@router.post("/preview")
async def preview_template(
    body: LabelPreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> StreamingResponse:
    """Render an unsaved design as the printer would.

    ⚠️ **The template travels in the body, not by id.** Dragging a box in the
    editor must not save anything, and the picture has to come from the renderer
    that will do the printing — a preview drawn in the browser is a second
    implementation of the layout, and it will disagree.

    Rendered at the device's own resolution and returned as a 1-bit PNG, so what
    the editor shows includes the things that only go wrong at 8 dots per
    millimetre: a QR whose modules fall under two dots, text that stops fitting.

    Any trouble comes back in ``X-Label-Warnings`` rather than as an error — a
    template with one bad element still has a picture worth looking at, and the
    header is what the editor turns into the warning strip.
    """
    spec = _as_spec(body.template)

    deeplink_base = f"{request.url.scheme}://{request.url.netloc}"
    if body.spool_id is not None:
        spool = await db.get(Spool, body.spool_id)
        if spool is None:
            raise HTTPException(404, f"Spool {body.spool_id} not found")
        context = spool_context(spool, deeplink_base=deeplink_base)
    else:
        context = example_context(deeplink_base=deeplink_base)

    png, warnings = render_template_png(spec, context, dots_per_mm=body.dots_per_mm)

    headers = {"Content-Length": str(len(png)), "Cache-Control": "no-store"}
    if warnings:
        # Header, not body: the response is an image, and a JSON envelope
        # carrying base64 would make the editor decode a picture to show it.
        headers["X-Label-Warnings"] = " | ".join(warnings)
        headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
    return StreamingResponse(io.BytesIO(png), media_type="image/png", headers=headers)
