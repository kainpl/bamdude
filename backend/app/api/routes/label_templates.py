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
    LabelSheetIn,
    LabelSheetOut,
    LabelSheetPreviewRequest,
    LabelTemplateIn,
    LabelTemplateOut,
    LabelTestPrintRequest,
)
from backend.app.services.label_context import example_context, spool_context
from backend.app.services.label_raster import render_template_png
from backend.app.services.label_template import (
    PLACEHOLDERS,
    LabelSheetSpec,
    LabelTemplateSpec,
    Placeholder,
    sheet_overflow,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/label-templates", tags=["label-templates"])


def _to_out(row: LabelTemplate) -> LabelTemplateOut:
    return LabelTemplateOut(
        id=row.id,
        name=row.name,
        width_mm=row.width_mm,
        height_mm=row.height_mm,
        shape=row.shape,
        target=row.target,
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


def _sheet_out(row: LabelSheet) -> LabelSheetOut:
    """One row as the editor sees it — including what does not fit.

    ⚠️ The overflow travels with every read, not only with a save. A geometry
    can stop fitting without anyone editing it: change the paper from A4 to A5
    and the same grid runs off the page. Recomputing on read is what makes that
    visible in the list instead of at the printer.
    """
    return LabelSheetOut(
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
        is_builtin=row.builtin_key is not None,
        overflow=_overflow_of(row),
    )


def _overflow_of(row: LabelSheet) -> list[str]:
    try:
        spec = LabelSheetSpec(
            name=row.name,
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
    except ValidationError:
        # A row the current spec cannot express — an old page size, say. Saying
        # nothing is right: the list still draws, and the editor refuses on save.
        return []
    return sheet_overflow(spec)


async def _load_sheet(db: AsyncSession, sheet_id: int) -> LabelSheet:
    row = await db.get(LabelSheet, sheet_id)
    if row is None:
        raise HTTPException(404, f"Sheet {sheet_id} not found")
    return row


def _as_sheet_spec(body: LabelSheetIn) -> LabelSheetSpec:
    """Validate a posted geometry, and refuse one that runs off its paper.

    ⚠️ Refused on save, not merely warned about: a grid wider than its page
    prints its last column half off the edge, and the discovery costs a sheet of
    adhesive stock. The editor sees the same sentences while drawing, so nothing
    arrives here as a surprise.
    """
    try:
        spec = LabelSheetSpec(**body.model_dump())
    except ValidationError as error:
        raise HTTPException(422, f"Invalid sheet: {error}") from error
    problems = sheet_overflow(spec)
    if problems:
        raise HTTPException(422, " ".join(problems))
    return spec


@router.get("/sheets", response_model=list[LabelSheetOut])
async def list_sheets(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> list[LabelSheetOut]:
    """Paper geometries, each carrying whatever about it does not fit."""
    result = await db.execute(select(LabelSheet).order_by(LabelSheet.name))
    return [_sheet_out(row) for row in result.scalars().all()]


@router.post("/sheets", response_model=LabelSheetOut, status_code=201)
async def create_sheet(
    body: LabelSheetIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelSheetOut:
    """A paper geometry of your own — for the sheet that is not in the list."""
    _as_sheet_spec(body)
    row = LabelSheet(**body.model_dump(), builtin_key=None)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _sheet_out(row)


@router.put("/sheets/{sheet_id}", response_model=LabelSheetOut)
async def update_sheet(
    sheet_id: int,
    body: LabelSheetIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelSheetOut:
    row = await _load_sheet(db, sheet_id)
    if row.builtin_key is not None:
        # ⚠️ Same rule as a built-in design, for the same reason: an automation
        # printing onto Avery 5160 for a year must not find the grid moved under
        # it. Duplicating is how you "edit" one.
        raise HTTPException(
            409,
            f"'{row.name}' is a built-in sheet and cannot be edited — duplicate it to make a copy you own.",
        )

    _as_sheet_spec(body)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return _sheet_out(row)


@router.post("/sheets/{sheet_id}/duplicate", response_model=LabelSheetOut, status_code=201)
async def duplicate_sheet(
    sheet_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> LabelSheetOut:
    source = await _load_sheet(db, sheet_id)
    copy = LabelSheet(
        name=f"{source.name} (copy)",
        builtin_key=None,  # the copy is yours; the key is what makes one read-only
        page_size=source.page_size,
        cell_width_mm=source.cell_width_mm,
        cell_height_mm=source.cell_height_mm,
        cols=source.cols,
        rows=source.rows,
        margin_top_mm=source.margin_top_mm,
        margin_left_mm=source.margin_left_mm,
        gap_x_mm=source.gap_x_mm,
        gap_y_mm=source.gap_y_mm,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return _sheet_out(copy)


@router.delete("/sheets/{sheet_id}", status_code=204, response_model=None)
async def delete_sheet(
    sheet_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_WRITE),
) -> None:
    row = await _load_sheet(db, sheet_id)
    if row.builtin_key is not None:
        raise HTTPException(409, f"'{row.name}' is a built-in sheet and cannot be deleted.")
    # ⚠️ Nothing references a sheet — that is the point of it not holding a
    # design — so there is no in-use check to make here.
    await db.delete(row)
    await db.commit()


@router.post("/sheets/preview")
async def preview_sheet(
    body: LabelSheetPreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_TEMPLATES_READ),
) -> StreamingResponse:
    """The whole page, with a design laid into every cell.

    ⚠️ **A page, not a label.** The template editor's preview answers "does this
    label look right"; this one answers the questions only the page can: does
    the grid fit the paper, and does the design fit a cell. Neither is visible
    on one enlarged sticker.

    ⚠️ The geometry travels in the body like the template preview's does, so
    dragging a number in the editor saves nothing. The design comes by id
    because it is already saved — you are laying an existing label onto paper.
    """
    from backend.app.services.label_renderer import render_template_sheet_pdf

    sheet = _as_sheet_spec(body.sheet)

    template = await _load(db, body.template_id)
    spec = LabelTemplateSpec(
        name=template.name,
        width_mm=template.width_mm,
        height_mm=template.height_mm,
        shape=template.shape,
        target=template.target,
        elements=template.elements,
    )

    deeplink_base = f"{request.url.scheme}://{request.url.netloc}"
    context = example_context(deeplink_base=deeplink_base)
    pdf, warnings = render_template_sheet_pdf(spec, [context] * sheet.per_page, sheet)

    if spec.width_mm > sheet.cell_width_mm or spec.height_mm > sheet.cell_height_mm:
        # ⚠️ Said rather than scaled. A design is printed at its own size or
        # refused — fractional scaling destroys bar ratios silently.
        warnings.append(
            f"'{spec.name}' is {spec.width_mm:.1f} × {spec.height_mm:.1f}mm and does not fit a "
            f"{sheet.cell_width_mm:.1f} × {sheet.cell_height_mm:.1f}mm cell."
        )

    headers = {"Content-Length": str(len(pdf)), "Cache-Control": "no-store"}
    if warnings:
        headers["X-Label-Warnings"] = " | ".join(warnings)
        headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf", headers=headers)


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
        target=body.target,
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
    row.target = body.target
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
        target=source.target,
        elements=list(source.elements or []),
        builtin_key=None,
        created_by=user.id if user else None,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return _to_out(copy)


# ``response_model=None`` is load-bearing under `from __future__ import
# annotations`: the ``-> None`` annotation reaches FastAPI as the string "None",
# which resolves to NoneType — a truthy class — and the app then fails at IMPORT
# on the fastapi 0.109-0.115 releases our requirements floor still allows.
# Pinned by test_204_routes_declare_response_model.
@router.delete("/{template_id}", status_code=204, response_model=None)
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


@router.post("/test-print", status_code=201)
async def test_print(
    body: LabelTestPrintRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.LABEL_JOBS_CREATE),
) -> dict[str, object]:
    """Print the design on screen, with example data, on a real printer.

    A preview answers "does it look right"; only stock answers "is the type
    readable, does the barcode scan, is it centred on the paper I actually
    have". This is the shortest path between the two, and it deliberately goes
    through the same gate, renderer and queue the real print does — a test that
    took a private route would prove nothing about the real one.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.label_device import LabelDevice
    from backend.app.services.label_dispatch import assert_fits, enqueue_jobs

    enabled = (await get_setting(db, "device_labels_enabled") or "").lower() == "true"
    if not enabled:
        raise HTTPException(409, "device_labels_disabled")

    device = await db.get(LabelDevice, body.device_id)
    if device is None:
        raise HTTPException(404, f"Label device {body.device_id} not found")
    if not device.enabled:
        raise HTTPException(
            409,
            f"'{device.name or device.installation_id}' has not been adopted — enable it before printing to it",
        )

    spec = _as_spec(body.template)
    assert_fits(spec, device)

    deeplink_base = f"{request.url.scheme}://{request.url.netloc}"
    context = example_context(deeplink_base=deeplink_base)

    jobs, warnings = await enqueue_jobs(
        db,
        device=device,
        spec=spec,
        contexts=[context],
        # ⚠️ No spool id. Nothing was printed *about* a spool, and recording one
        # would put a test label in that spool's history.
        spool_ids=[None],
        template_id=None,
        copies=1,
        user_id=user.id if user else None,
    )
    return {"job_id": jobs[0].id, "warnings": warnings}
