"""Spool label printing routes (B.1 — port of upstream Bambuddy #809).

Two endpoints, one per inventory backend:

- ``POST /inventory/labels``  — local-DB spools
- ``POST /spoolman/labels``   — Spoolman-backed spools

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
from backend.app.services.label_context import spool_context, spoolman_context
from backend.app.services.label_renderer import render_template_pdf, render_template_sheet_pdf
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES, sheet_cell_template
from backend.app.services.label_template import LabelSheetSpec, LabelTemplateSpec, resolve
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

    @model_validator(mode="after")
    def _exactly_one_way_of_naming_a_design(self) -> LabelRequest:
        """⚠️ Two answers to one question. Guessing which the caller meant is
        worse than saying so — the wrong guess prints a batch of wrong labels.
        """
        if self.template and self.template_id is not None:
            raise ValueError("name either 'template' or 'template_id', not both")
        if not self.template and self.template_id is None:
            raise ValueError("one of 'template' or 'template_id' is required")
        if self.sheet_id is not None and self.template_id is None:
            raise ValueError("'sheet_id' needs a 'template_id' to lay out on it")
        return self


async def _resolve_design(db: AsyncSession, body: LabelRequest) -> tuple[LabelTemplateSpec, LabelSheetSpec | None]:
    """Work out which design, and which paper if any, this request means.

    ⚠️ The seed constants are the fallback when a row is missing. That cannot
    happen on an install whose migrations ran — but it keeps the endpoint from
    depending on seed state, and a built-in cannot be edited, so the row and the
    constant can never disagree.
    """
    if body.template_id is not None:
        row = await db.get(LabelTemplate, body.template_id)
        if row is None:
            raise HTTPException(404, f"Label template {body.template_id} not found")
        spec = LabelTemplateSpec(
            name=row.name,
            width_mm=row.width_mm,
            height_mm=row.height_mm,
            shape=row.shape,
            elements=row.elements or [],
        )

        if body.sheet_id is None:
            return spec, None

        sheet_row = await db.get(LabelSheet, body.sheet_id)
        if sheet_row is None:
            raise HTTPException(404, f"Label sheet {body.sheet_id} not found")
        sheet = LabelSheetSpec(
            name=sheet_row.name,
            page_size=sheet_row.page_size,
            cell_width_mm=sheet_row.cell_width_mm,
            cell_height_mm=sheet_row.cell_height_mm,
            cols=sheet_row.cols,
            rows=sheet_row.rows,
            margin_top_mm=sheet_row.margin_top_mm,
            margin_left_mm=sheet_row.margin_left_mm,
            gap_x_mm=sheet_row.gap_x_mm,
            gap_y_mm=sheet_row.gap_y_mm,
        )
        if spec.width_mm > sheet.cell_width_mm + 1e-6 or spec.height_mm > sheet.cell_height_mm + 1e-6:
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
        return LabelTemplateSpec(**raw), None

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
    return LabelTemplateSpec(**sheet_cell_template(source)), LabelSheetSpec(**sheet_raw)


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


def _render(
    spec: LabelTemplateSpec,
    sheet: LabelSheetSpec | None,
    contexts: list[dict[str, str]],
    *,
    monochrome: bool,
) -> tuple[bytes, list[str]]:
    if monochrome:
        spec = _without_swatches(spec)
    if sheet is not None:
        return render_template_sheet_pdf(spec, contexts, sheet)
    return render_template_pdf(spec, contexts)


def _filename(spec: LabelTemplateSpec, sheet: LabelSheetSpec | None, prefix: str = "") -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (sheet.name if sheet else spec.name))
    return f"bamdude-labels-{prefix}{stem.strip('-').lower()}.pdf"


@router.post("/inventory/labels")
async def render_local_inventory_labels(
    body: LabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Render labels for spools in the local inventory."""
    spec, sheet = await _resolve_design(db, body)

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
    naming = (await get_setting(db, "spool_display_template") or "").strip()

    contexts = []
    for spool in ordered:
        override = name_overrides.get(spool.id)
        context = spool_context(spool, deeplink_base=deeplink_base, display_name=override)
        if not (override or "").strip():
            context = _apply_naming_template(context, naming)
        contexts.append(context)

    pdf, warnings = _render(spec, sheet, contexts, monochrome=body.monochrome)
    return _stream_pdf(pdf, _filename(spec, sheet), warnings)


@router.post("/spoolman/labels")
async def render_spoolman_labels(
    body: LabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> StreamingResponse:
    """Render labels for spools tracked in Spoolman.

    The Spoolman client doesn't expose a per-id endpoint, so this fetches the
    full spool list and filters in-memory. For typical libraries (~50 spools)
    that's negligible; for very large libraries this is the trade-off until
    Spoolman gains a bulk filter.
    """
    spec, sheet = await _resolve_design(db, body)

    spoolman_on = (await get_setting(db, "spoolman_enabled") or "").lower() == "true"
    if not spoolman_on:
        raise HTTPException(400, "Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if client is None or not client.is_connected:
        raise HTTPException(503, "Spoolman not reachable")

    try:
        all_spools = await client.get_spools()
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
    naming = (await get_setting(db, "spool_display_template") or "").strip()

    contexts = []
    for sid in requested_ids:
        override = name_overrides.get(sid)
        context = spoolman_context(by_id[sid], deeplink_base=deeplink_base, display_name=override)
        if not (override or "").strip():
            context = _apply_naming_template(context, naming)
        contexts.append(context)

    pdf, warnings = _render(spec, sheet, contexts, monochrome=body.monochrome)
    return _stream_pdf(pdf, _filename(spec, sheet, prefix="spoolman-"), warnings)
