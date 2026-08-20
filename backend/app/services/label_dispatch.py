"""Turning spools into queued pictures for a bridge-attached printer.

The rendering itself belongs to ``label_raster``; the placeholder values belong
to ``label_context``. What lives here is the part neither of them can answer:
which design, at what size, and whether the paper currently in the printer can
take it.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelCassette, LabelDevice, LabelJob
from backend.app.models.label_template import LabelTemplate
from backend.app.models.spool import Spool
from backend.app.schemas.label_device import LabelSpoolEntry
from backend.app.services.label_context import spool_context
from backend.app.services.label_raster import render_template_png
from backend.app.services.label_template import LabelTemplateSpec, resolve

logger = logging.getLogger(__name__)

#: 203 dpi. Every Niimbot the bridge speaks to is 8 dots per millimetre; when a
#: 300 dpi model appears this becomes a device column, not a constant.
DOTS_PER_MM = 8.0

#: How far a design may exceed the loaded stock before it is refused, in mm.
#: ⚠️ Not zero: a catalogue entry typed as 50 and stock sold as 50.0 differ by
#: rounding often enough that an exact comparison would refuse correct paper.
_SIZE_TOLERANCE_MM = 0.5


def _spec_of(row: LabelTemplate) -> LabelTemplateSpec:
    return LabelTemplateSpec(
        name=row.name,
        width_mm=row.width_mm,
        height_mm=row.height_mm,
        shape=row.shape,
        elements=row.elements or [],
    )


async def resolve_cassette(db: AsyncSession, barcode: str | None) -> tuple[float, float] | None:
    """What size the stock with this barcode is, if anybody has said.

    ⚠️ **No call to any vendor cloud.** A self-hosted install does not quietly
    send consumable identifiers to a third party; the catalogue is taught by the
    person holding the cassette.
    """
    if not barcode:
        return None
    row = (await db.execute(select(LabelCassette).where(LabelCassette.barcode == barcode))).scalar_one_or_none()
    if row is None:
        return None
    return row.width_mm, row.height_mm


async def resolve_template(
    db: AsyncSession,
    device: LabelDevice,
    template_id: int | None,
) -> LabelTemplateSpec:
    """Which design to draw, checked against the paper that is actually loaded.

    ⚠️ **The template is the truth and the cassette is the gate.** The design
    states its size in millimetres and is printed at exactly that size — never
    scaled to fit. Fractional scaling of a 1-bit raster destroys the bar-width
    ratios a scanner reads by, and the failure is silent: the label looks fine
    and simply will not scan.

    So a design larger than the loaded stock is refused, with both sizes in the
    message, and a request that names no design gets one that matches the
    cassette. If nothing matches, that is also a refusal — printing a guess onto
    somebody's stock is worse than asking.
    """
    cassette = (
        (device.cassette_width_mm, device.cassette_height_mm)
        if device.cassette_width_mm and device.cassette_height_mm
        else None
    )

    if template_id is not None:
        row = await db.get(LabelTemplate, template_id)
        if row is None:
            raise HTTPException(404, f"Label template {template_id} not found")
        spec = _spec_of(row)
        if cassette is not None:
            width, height = cassette
            if spec.width_mm > width + _SIZE_TOLERANCE_MM or spec.height_mm > height + _SIZE_TOLERANCE_MM:
                raise HTTPException(
                    422,
                    f"'{spec.name}' is {spec.width_mm:g} x {spec.height_mm:g} mm and does not fit the "
                    f"{width:g} x {height:g} mm stock loaded in this printer",
                )
        return spec

    if cassette is None:
        raise HTTPException(
            422,
            "This printer has not reported what stock is loaded, so there is nothing to pick a design "
            "against — name a template_id, or teach the cassette barcode its size",
        )

    width, height = cassette
    rows = (await db.execute(select(LabelTemplate))).scalars().all()
    fitting = [
        row
        for row in rows
        if abs(row.width_mm - width) <= _SIZE_TOLERANCE_MM and abs(row.height_mm - height) <= _SIZE_TOLERANCE_MM
    ]
    if not fitting:
        raise HTTPException(
            422,
            f"No label design is {width:g} x {height:g} mm, which is what this printer has loaded — "
            "make one that size, or name a template_id that fits",
        )
    # A design somebody made beats a built-in of the same size: if they went to
    # the trouble of drawing one for this stock, that is the one they mean.
    fitting.sort(key=lambda row: (row.builtin_key is not None, row.name))
    return _spec_of(fitting[0])


async def build_contexts(
    db: AsyncSession,
    entries: list[LabelSpoolEntry],
    *,
    deeplink_base: str,
    naming_template: str,
) -> list[dict[str, str]]:
    """Placeholder values for each spool, in the order they were asked for.

    Shared with the PDF path on purpose — a device label and a printed one must
    say the same thing about the same spool, and two builders is how they stop.
    """
    requested = [entry.id for entry in entries]
    overrides = {entry.id: entry.display_name for entry in entries}

    spools = (await db.execute(select(Spool).where(Spool.id.in_(requested)))).scalars().all()
    by_id = {spool.id: spool for spool in spools}
    missing = [sid for sid in requested if sid not in by_id]
    if missing:
        raise HTTPException(404, f"Spool(s) not found: {missing}")

    contexts = []
    for sid in requested:
        override = (overrides.get(sid) or "").strip()
        context = spool_context(by_id[sid], deeplink_base=deeplink_base, display_name=override or None)
        if not override and naming_template:
            composed = resolve(naming_template, context).strip()
            if composed:
                context = {**context, "display_name": composed}
        contexts.append(context)
    return contexts


def render_job_png(spec: LabelTemplateSpec, context: dict[str, str]) -> tuple[bytes, list[str]]:
    return render_template_png(spec, context, dots_per_mm=DOTS_PER_MM)


async def enqueue_jobs(
    db: AsyncSession,
    *,
    device: LabelDevice,
    spec: LabelTemplateSpec,
    contexts: list[dict[str, str]],
    spool_ids: list[int],
    template_id: int | None,
    copies: int,
    user_id: int | None,
) -> tuple[list[LabelJob], list[str]]:
    """One queued job per spool, each carrying its finished picture.

    ⚠️ Drawn here, not when the device comes for it. The queue can sit for hours
    on a desktop that is switched off, and the label that comes out has to be
    the one the operator looked at.
    """
    jobs: list[LabelJob] = []
    warnings: list[str] = []
    for spool_id, context in zip(spool_ids, contexts, strict=True):
        png, found = render_job_png(spec, context)
        for warning in found:
            if warning not in warnings:
                warnings.append(warning)
        job = LabelJob(
            device_id=device.id,
            spool_id=spool_id,
            template_id=template_id,
            width_mm=spec.width_mm,
            height_mm=spec.height_mm,
            copies=copies,
            image_png=png,
            status="queued",
            created_by=user_id,
        )
        db.add(job)
        jobs.append(job)

    await db.commit()
    for job in jobs:
        await db.refresh(job)
    return jobs, warnings


__all__ = [
    "DOTS_PER_MM",
    "build_contexts",
    "enqueue_jobs",
    "render_job_png",
    "resolve_cassette",
    "resolve_template",
]
