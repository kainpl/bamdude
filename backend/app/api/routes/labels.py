"""Spool label printing routes (B.1 — port of upstream Bambuddy #809).

Two endpoints, one per inventory backend:

- ``POST /inventory/labels``  — local-DB spools
- ``POST /spoolman/labels``   — Spoolman-backed spools

Each has a ``/png`` twin that takes the same body plus ``dpi`` and answers the
labels as pictures (one PNG, or a ZIP of them) — see ``services/label_picture``.

Both accept ``{spools: [{id, display_name?}], template}`` and return a PDF
stream. The QR code on each label deep-links to ``/inventory?spool=<id>`` so
a phone scan jumps straight back into BamDude at that spool's row.

**The layouts are templates now.** The six names these endpoints have always
taken resolve to seeded rows — four designs and two papers — so a caller that
knows nothing about templates notices nothing. ``template_id`` names a design
directly, and pairing it with ``sheet_id`` prints any design on any paper,
which the six fixed names could never express.

⚠️ **Labels do not come out pixel-identical to the fixed layouts.** Those
adjusted themselves in ways a movable design cannot: dropping rows that would
collide, sizing the QR against a floor. The seed reproduces their geometry from
the same formulas, so the difference is small — but it is real, and it is in the
CHANGELOG.

The optional ``display_name`` per spool lets the frontend forward whatever
``formatSpoolDisplayName`` produced from the user-configurable
``spool_display_template`` setting — that way the bold central line on the
label matches what the operator sees on the Inventory page. ⚠️ When it is
missing, the *server* interpolates that same setting rather than falling back to
a fixed chain: a caller with no browser — an API key, a Telegram action — must
get the same label the page would have printed, not a different one.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting
from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.services import label_picture
from backend.app.services.inventory_service import spool_display_template
from backend.app.services.label_context import spool_context, spoolman_context
from backend.app.services.label_gate import checked_design, checked_sheet, design_of, draw, sheet_of
from backend.app.services.label_renderer import render_template_pdf, render_template_sheet_pdf
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES, sheet_cell_template
from backend.app.services.label_template import LabelSheetSpec, LabelTemplateSpec, fits_within, resolve
from backend.app.services.spoolman import get_spoolman_client
from backend.app.utils.http import build_content_disposition

logger = logging.getLogger(__name__)

router = APIRouter(tags=["labels"])

#: The four names that describe a single label.
_LABEL_NAMES = ("ams_holder_74x33", "ams_holder_75x55", "box_40x30", "box_62x29")
#: The two that describe a page of them.
_SHEET_NAMES = ("avery_5160", "avery_l7160")

# Cap how many labels can be requested in one go. Sane upper bound for the
# largest realistic batch (an Avery sheet at 30/page × ~10 pages).
MAX_LABELS_PER_REQUEST = 500


class LabelSpoolEntry(BaseModel):
    """One spool to render. ``display_name`` is the optional override for the
    label's bold central line — when provided it replaces the server-side
    interpolation of ``spool_display_template``. Frontend posts the value
    composed by ``formatSpoolDisplayName`` so the label matches the user's
    Inventory naming-template setting.
    """

    id: int
    display_name: str | None = None


class LabelRequest(BaseModel):
    spools: list[LabelSpoolEntry] = Field(..., min_length=1, max_length=MAX_LABELS_PER_REQUEST)
    #: One of the six names this endpoint has always taken. Kept as a literal
    #: so a typo is still refused before anything is loaded.
    template: (
        Literal[
            "ams_holder_74x33",
            "ams_holder_75x55",
            "box_40x30",
            "box_62x29",
            "avery_5160",
            "avery_l7160",
        ]
        | None
    ) = None
    #: A design from the catalogue, by id.
    template_id: int | None = None
    #: Paper to lay that design out on. Only meaningful beside ``template_id``.
    sheet_id: int | None = None
    # Black-and-white thermal printers: drop the colour swatch (prints as a
    # muddy grey block) and leave the text where it is (#1870).
    monochrome: bool = False
    #: The first free cell of a half-used sheet, 1-based, row by row (upstream
    #: #2879). Only a sheet has cells; the range is checked once the paper is known.
    starting_position: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _exactly_one_way_of_naming_a_design(self) -> LabelRequest:
        """⚠️ Two answers to one question. Guessing which the caller meant is
        worse than saying so — the wrong guess prints a batch of wrong labels.
        """
        if self.template and self.template_id is not None:
            raise ValueError("name either 'template' or 'template_id', not both")
        # ⚠️ Two of the six names ARE sheets, so a name beside a sheet id is a
        # request that answers "which paper" twice — and the quiet outcome is
        # a batch printed on the wrong one. Ask by id if you want to choose.
        if self.template and self.sheet_id is not None:
            raise ValueError("name either 'template' or 'sheet_id', not both — a name already says which paper")
        if not self.template and self.template_id is None and self.sheet_id is None:
            raise ValueError("one of 'template', 'template_id' or 'sheet_id' is required")
        return self


class LabelPictureRequest(LabelRequest):
    """The same selection, saved as pictures instead of printed."""

    #: Dots per inch of the picture: the two thermal heads in common use and an
    #: office printer. The PNG records it, so it prints at the design's own size.
    dpi: Literal[203, 300, 600] = 300


async def _resolve_design(db: AsyncSession, body: LabelRequest) -> tuple[LabelTemplateSpec, LabelSheetSpec | None]:
    """Work out which design, and which paper if any, this request means.

    ⚠️ The seed constants are the fallback when a row is missing, which keeps
    the endpoint from depending on seed state.

    The row wins whenever there is one, and since the seeded designs became
    editable the two CAN now disagree — deliberately. Somebody who redraws the
    box label means the box label to be that, including for the automation
    posting ``{"template": "box_40x30"}``. Deleting the row is what puts the
    shipped one back.
    """
    if body.template_id is None and body.sheet_id is not None:
        # ⚠️ A sheet on its own is answerable, and used to be: the six legacy
        # names include two sheets, and each builds its design TO THE CELL
        # rather than pointing at a template row. Refusing the same thing when
        # it arrives as an id would have made the newer, catalogue-driven path
        # less capable than the names it replaces — and forced the picker to ask
        # for a design before it can offer "print on Avery 5160".
        sheet_row = await db.get(LabelSheet, body.sheet_id)
        if sheet_row is None:
            raise HTTPException(404, f"Label sheet {body.sheet_id} not found")
        sheet = sheet_of(sheet_row)
        return _cell_design(sheet_row, sheet), sheet

    if body.template_id is not None:
        row = await db.get(LabelTemplate, body.template_id)
        if row is None:
            raise HTTPException(404, f"Label template {body.template_id} not found")
        spec = design_of(row)

        if body.sheet_id is None:
            return spec, None

        sheet_row = await db.get(LabelSheet, body.sheet_id)
        if sheet_row is None:
            raise HTTPException(404, f"Label sheet {body.sheet_id} not found")
        sheet = sheet_of(sheet_row)
        if not fits_within(spec.width_mm, spec.height_mm, sheet.cell_width_mm, sheet.cell_height_mm):
            raise HTTPException(
                400,
                f"'{spec.name}' is {spec.width_mm:g} × {spec.height_mm:g} mm and does not fit "
                f"a {sheet.cell_width_mm:g} × {sheet.cell_height_mm:g} mm cell of '{sheet.name}'",
            )
        return spec, sheet

    if body.template in _LABEL_NAMES:
        row = (
            await db.execute(select(LabelTemplate).where(LabelTemplate.builtin_key == body.template))
        ).scalar_one_or_none()
        raw: dict[str, Any] = (
            {
                "name": row.name,
                "width_mm": row.width_mm,
                "height_mm": row.height_mm,
                "shape": row.shape,
                "elements": row.elements or [],
            }
            if row is not None
            else dict(next(t for t in BUILTIN_TEMPLATES if t["builtin_key"] == body.template))
        )
        raw.pop("builtin_key", None)
        return checked_design(raw), None

    assert body.template in _SHEET_NAMES  # the Literal admits nothing else
    # A sheet name. It describes a page and never a design, so the design is
    # built to the cell — there is no template row for it to point at, and
    # seeding one per sheet would put undeletable rows in the catalogue whose
    # only purpose is to be the inside of a page.
    sheet_row = (
        await db.execute(select(LabelSheet).where(LabelSheet.builtin_key == body.template))
    ).scalar_one_or_none()
    source: Any = (
        sheet_row if sheet_row is not None else next(s for s in BUILTIN_SHEETS if s["builtin_key"] == body.template)
    )
    sheet_raw = (
        {key: value for key, value in source.items() if key != "builtin_key"}
        if isinstance(source, dict)
        else {
            "name": source.name,
            "page_size": source.page_size,
            "cell_width_mm": source.cell_width_mm,
            "cell_height_mm": source.cell_height_mm,
            "cols": source.cols,
            "rows": source.rows,
            "margin_top_mm": source.margin_top_mm,
            "margin_left_mm": source.margin_left_mm,
            "gap_x_mm": source.gap_x_mm,
            "gap_y_mm": source.gap_y_mm,
        }
    )
    sheet = checked_sheet(sheet_raw)
    return _cell_design(source, sheet), sheet


def _cell_design(source: Any, sheet: LabelSheetSpec) -> LabelTemplateSpec:
    """The built-in layout, drawn to one cell of ``sheet``.

    ⚠️ That layout needs room: a swatch, a text column and a QR side by side.
    In a cell too narrow for the three the text column comes out with a negative
    width, which used to surface as a 500. A sheet of small cells is still
    printable — with a design drawn for them.
    """
    try:
        return checked_design(sheet_cell_template(source))
    except HTTPException as refusal:
        raise HTTPException(
            422,
            f"The cells of '{sheet.name}' are too small for the built-in layout — choose a design that fits them",
        ) from refusal


def _without_swatches(spec: LabelTemplateSpec) -> LabelTemplateSpec:
    """⚠️ Monochrome drops the colour block and leaves everything else put.

    The fixed layout also widened the text column into the freed space; a design
    whose boxes a person placed cannot be rearranged behind their back, so this
    leaves a gap where the swatch was. The hex line still carries the colour
    (#1870), which is what made the swatch droppable in the first place.
    """
    return spec.model_copy(update={"elements": [e for e in spec.elements if e.type != "swatch"]})


async def _resolve_deeplink_base(request: Request, db: AsyncSession) -> str:
    """Where the QR codes should point. Prefers ``external_url`` setting when
    set so a phone scan reaches the user's public BamDude URL rather than an
    internal address; falls back to ``APP_URL`` env then to the request's own
    scheme+host.
    """
    external = (await get_setting(db, "external_url") or "").strip().rstrip("/")
    if external:
        return external
    env_url = (os.environ.get("APP_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url
    return f"{request.url.scheme}://{request.url.netloc}"


def _apply_naming_template(context: dict[str, str], naming: str) -> dict[str, str]:
    """Interpolate the user's spool-naming setting into ``display_name``.

    ⚠️ Server-side, against the context that was just built. The frontend does
    the same substitution against the same tokens, so a label printed by an API
    key reads exactly like one printed from the page — which was not true while
    the backend had its own fallback chain and the browser had the setting.

    A template that resolves to nothing leaves whatever the builder worked out,
    rather than printing a label with no name on it.
    """
    if not naming:
        return context
    composed = resolve(naming, context).strip()
    if composed:
        context = {**context, "display_name": composed}
    return context


def _stream_pdf(pdf: bytes, filename: str, warnings: list[str]) -> StreamingResponse:
    headers = {
        "Content-Disposition": build_content_disposition(filename, disposition="inline"),
        "Content-Length": str(len(pdf)),
        # PDFs are deterministic per request; tell the browser not to cache
        # so re-printing after edits picks up the new data.
        "Cache-Control": "no-store",
    }
    if warnings:
        # ⚠️ A header, and the PDF still arrives. A design with one bad element
        # — a barcode whose payload will not encode — still prints every other
        # field, and refusing the batch would leave a shelf unlabelled over one
        # empty column.
        headers["X-Label-Warnings"] = " | ".join(warnings)
        headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
        logger.info("label render warnings: %s", "; ".join(warnings))
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf", headers=headers)


def _check_starting_position(body: LabelRequest, sheet: LabelSheetSpec | None) -> None:
    """A starting cell exists only on a sheet, and only up to the sheet's last one."""
    if body.starting_position == 1:
        return
    if sheet is None:
        raise HTTPException(422, "A starting position applies only to a sheet of labels")
    if body.starting_position > sheet.per_page:
        raise HTTPException(422, f"The starting position must be between 1 and {sheet.per_page} on this sheet")


def _render(
    spec: LabelTemplateSpec,
    sheet: LabelSheetSpec | None,
    contexts: list[dict[str, str]],
    *,
    monochrome: bool,
    starting_position: int = 1,
) -> tuple[bytes, list[str]]:
    if monochrome:
        spec = _without_swatches(spec)
    if sheet is not None:
        return render_template_sheet_pdf(spec, contexts, sheet, starting_position=starting_position)
    return render_template_pdf(spec, contexts)


def _filename(spec: LabelTemplateSpec, sheet: LabelSheetSpec | None, prefix: str = "") -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (sheet.name if sheet else spec.name))
    return f"bamdude-labels-{prefix}{stem.strip('-').lower()}.pdf"


async def _local_labels(db: AsyncSession, request: Request, body: LabelRequest) -> list[tuple[int, dict[str, str]]]:
    """Each local spool of the request as ``(id, what its label says)``, in the order asked.

    ⚠️ One builder for the PDF and the pictures: a preview that filled its label
    in some other way would show a label the print does not make.
    """
    requested_ids = [entry.id for entry in body.spools]
    name_overrides = {entry.id: entry.display_name for entry in body.spools}

    result = await db.execute(select(Spool).where(Spool.id.in_(requested_ids)))
    spools = list(result.scalars().all())

    found_ids = {s.id for s in spools}
    missing = [sid for sid in requested_ids if sid not in found_ids]
    if missing:
        raise HTTPException(404, f"Spool(s) not found: {missing}")

    # Preserve caller's order so an Avery sheet print matches the on-screen list.
    ordered = sorted(spools, key=lambda s: requested_ids.index(s.id))

    deeplink_base = await _resolve_deeplink_base(request, db)
    naming = await spool_display_template(db)

    labels = []
    for spool in ordered:
        override = name_overrides.get(spool.id)
        context = spool_context(spool, deeplink_base=deeplink_base, display_name=override)
        if not (override or "").strip():
            context = _apply_naming_template(context, naming)
        labels.append((spool.id, context))
    return labels


async def _spoolman_labels(db: AsyncSession, request: Request, body: LabelRequest) -> list[tuple[int, dict[str, str]]]:
    """The same for spools tracked in Spoolman.

    The Spoolman client doesn't expose a per-id endpoint, so this fetches the
    full spool list and filters in-memory. For typical libraries (~50 spools)
    that's negligible; for very large libraries this is the trade-off until
    Spoolman gains a bulk filter.
    """
    spoolman_on = (await get_setting(db, "spoolman_enabled") or "").lower() == "true"
    if not spoolman_on:
        raise HTTPException(400, "Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if client is None or not client.is_connected:
        raise HTTPException(503, "Spoolman not reachable")

    try:
        # Archived ones too: the Inventory page shows and selects them, and one
        # in the batch failed the whole print with a 404 (upstream c2bc41a5e).
        all_spools = await client.get_all_spools(allow_archived=True)
    except Exception as exc:
        logger.warning("Spoolman fetch failed during label render: %s", exc)
        raise HTTPException(502, "Failed to fetch spools from Spoolman") from exc

    requested_ids = [entry.id for entry in body.spools]
    name_overrides = {entry.id: entry.display_name for entry in body.spools}

    by_id = {int(s.get("id", 0)): s for s in all_spools if s.get("id") is not None}
    missing = [sid for sid in requested_ids if sid not in by_id]
    if missing:
        raise HTTPException(404, f"Spool(s) not found in Spoolman: {missing}")

    deeplink_base = await _resolve_deeplink_base(request, db)
    naming = await spool_display_template(db)

    labels = []
    for sid in requested_ids:
        override = name_overrides.get(sid)
        context = spoolman_context(by_id[sid], deeplink_base=deeplink_base, display_name=override)
        if not (override or "").strip():
            context = _apply_naming_template(context, naming)
        labels.append((sid, context))
    return labels


async def _save_as_pictures(
    db: AsyncSession, request: Request, body: LabelPictureRequest, labels_of: Any
) -> StreamingResponse:
    """One PNG for one label, a ZIP for several.

    ⚠️ **The label, never the page.** A sheet is paper for a driver; software
    that takes a picture takes one label at a time. So a design chosen with a
    sheet is saved label by label at its own size, and the starting cell — a
    fact about the paper — says nothing here.
    """
    spec, _sheet = await _resolve_design(db, body)
    if body.monochrome:
        spec = _without_swatches(spec)
    # Before the spools are read: the answer depends on nothing they hold.
    label_picture.checked(spec, body.dpi, labels=len(body.spools))

    labels = await labels_of(db, request, body)
    names = label_picture.picture_names([(number, context.get("display_name", "")) for number, context in labels])
    try:
        pictures, warnings = await label_picture.label_pictures(spec, [context for _, context in labels], dpi=body.dpi)
        if len(pictures) == 1:
            content, media_type = pictures[0], "image/png"
            disposition = build_content_disposition(names[0], ascii_fallback=f"label-{labels[0][0]:04d}.png")
        else:
            content, media_type = await draw(label_picture.bundle, names, pictures), "application/zip"
            disposition = build_content_disposition(f"{_filename(spec, None)[:-4]}-{body.dpi}dpi.zip")
    except label_picture.BundleTooLarge:
        raise HTTPException(
            413,
            f"These labels come to more than {label_picture.MAX_BUNDLE_BYTES // (1024 * 1024)} MB as pictures — "
            "save fewer at a time or choose a lower resolution",
        ) from None

    headers = {"Content-Disposition": disposition, "Content-Length": str(len(content)), "Cache-Control": "no-store"}
    if warnings:
        headers["X-Label-Warnings"] = " | ".join(warnings)
        headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
    return StreamingResponse(io.BytesIO(content), media_type=media_type, headers=headers)


@router.post("/inventory/labels/png")
async def save_local_inventory_labels_as_pictures(
    body: LabelPictureRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Labels of local spools as pictures — whoever may print them may save them."""
    return await _save_as_pictures(db, request, body, _local_labels)


@router.post("/spoolman/labels/png")
async def save_spoolman_labels_as_pictures(
    body: LabelPictureRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Labels of Spoolman spools as pictures."""
    return await _save_as_pictures(db, request, body, _spoolman_labels)


@router.post("/inventory/labels")
async def render_local_inventory_labels(
    body: LabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Render labels for spools in the local inventory."""
    spec, sheet = await _resolve_design(db, body)
    _check_starting_position(body, sheet)

    contexts = [context for _, context in await _local_labels(db, request, body)]

    pdf, warnings = await draw(
        _render, spec, sheet, contexts, monochrome=body.monochrome, starting_position=body.starting_position
    )
    return _stream_pdf(pdf, _filename(spec, sheet), warnings)


@router.post("/spoolman/labels")
async def render_spoolman_labels(
    body: LabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Render labels for spools tracked in Spoolman."""
    spec, sheet = await _resolve_design(db, body)
    _check_starting_position(body, sheet)

    contexts = [context for _, context in await _spoolman_labels(db, request, body)]

    pdf, warnings = await draw(
        _render, spec, sheet, contexts, monochrome=body.monochrome, starting_position=body.starting_position
    )
    return _stream_pdf(pdf, _filename(spec, sheet, prefix="spoolman-"), warnings)
