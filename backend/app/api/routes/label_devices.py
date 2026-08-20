"""Printing a label on a printer attached to somebody's desktop.

A container cannot reach a USB printer, but the desktop process can reach the
container. So the server owns a queue and the bridge asks for work over plain
HTTP — there is no inbound connection to a desktop anywhere in this design, and
no device protocol here at all.

The subsystem is off by default. Every route in this module refuses with 409
while it is, because a queue nothing will ever drain is worse than an absent
feature: it looks like it is working.
"""

from __future__ import annotations

import base64
import io
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting
from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.label_device import LabelCassette, LabelDevice, LabelJob
from backend.app.models.user import User
from backend.app.schemas.label_device import (
    LabelCassetteIn,
    LabelCassetteOut,
    LabelDeviceOut,
    LabelDeviceReport,
    LabelDeviceUpdate,
    LabelJobCreate,
    LabelJobHandout,
    LabelJobOut,
    LabelJobPreview,
    LabelJobResult,
)
from backend.app.services.label_dispatch import (
    DOTS_PER_MM,
    build_contexts,
    claim_next_job,
    enqueue_jobs,
    render_job_png,
    resolve_cassette,
    resolve_template,
    upsert_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["label-devices"])

#: How long a bridge waits before asking again when there was nothing for it.
POLL_INTERVAL_SECONDS = 5
#: And when the whole subsystem is switched off. Long, because the answer will
#: not change until somebody visits a settings page.
DISABLED_BACKOFF_SECONDS = 300


async def _require_subsystem(db: AsyncSession) -> None:
    enabled = (await get_setting(db, "device_labels_enabled") or "").lower() == "true"
    if not enabled:
        raise HTTPException(409, "device_labels_disabled")


async def _load_device(db: AsyncSession, device_id: int) -> LabelDevice:
    device = await db.get(LabelDevice, device_id)
    if device is None:
        raise HTTPException(404, f"Label device {device_id} not found")
    return device


async def _resolve_deeplink_base(request: Request, db: AsyncSession) -> str:
    """Where a label's QR points. Same order the PDF path uses, for the same
    reason: a phone scan should reach the install's public address.
    """
    external = (await get_setting(db, "external_url") or "").strip().rstrip("/")
    if external:
        return external
    env_url = (os.environ.get("APP_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url
    return f"{request.url.scheme}://{request.url.netloc}"


def _device_out(device: LabelDevice, queued: int = 0) -> LabelDeviceOut:
    return LabelDeviceOut(
        id=device.id,
        installation_id=device.installation_id,
        driver=device.driver,
        model=device.model,
        protocol_version=device.protocol_version,
        transport=device.transport,
        address=device.address,
        name=device.name,
        enabled=device.enabled,
        density=device.density,
        app_version=device.app_version,
        last_seen_at=device.last_seen_at,
        cassette_barcode=device.cassette_barcode,
        cassette_width_mm=device.cassette_width_mm,
        cassette_height_mm=device.cassette_height_mm,
        paper_state=device.paper_state,
        power_level=device.power_level,
        printer_reachable=device.printer_reachable,
        queued=queued,
    )


def _job_out(job: LabelJob) -> LabelJobOut:
    return LabelJobOut(
        id=job.id,
        device_id=job.device_id,
        spool_id=job.spool_id,
        template_id=job.template_id,
        width_mm=job.width_mm,
        height_mm=job.height_mm,
        copies=job.copies,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        claimed_at=job.claimed_at,
        created_at=job.created_at,
    )


# ── Devices ──────────────────────────────────────────────────────────────────


@router.get("/label-devices", response_model=list[LabelDeviceOut])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_READ),
) -> list[LabelDeviceOut]:
    """Every bridge that has ever introduced itself, adopted or not.

    ⚠️ A device nobody has adopted is listed rather than hidden — it is exactly
    the row somebody needs to find in order to adopt it.
    """
    devices = (await db.execute(select(LabelDevice).order_by(LabelDevice.id))).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(LabelJob.device_id, func.count(LabelJob.id))
                .where(LabelJob.status == "queued")
                .group_by(LabelJob.device_id)
            )
        ).all()
    )
    return [_device_out(device, counts.get(device.id, 0)) for device in devices]


@router.patch("/label-devices/{device_id}", response_model=LabelDeviceOut)
async def update_device(
    device_id: int,
    body: LabelDeviceUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_MANAGE),
) -> LabelDeviceOut:
    """Adopt a device, name it, or set how dark it prints.

    ⚠️ Adopting is the whole point of this route. Everything else about a device
    is reported by the device; these three fields are the only ones a person
    owns, and enabling one is the decision that a machine on somebody's desk may
    receive our labels.
    """
    device = await _load_device(db, device_id)
    if body.name is not None:
        device.name = body.name.strip() or None
    if body.enabled is not None:
        device.enabled = body.enabled
    if body.density is not None:
        device.density = body.density
    await db.commit()
    await db.refresh(device)
    return _device_out(device)


@router.delete("/label-devices/{device_id}", status_code=204, response_model=None)
async def delete_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_MANAGE),
) -> None:
    """Forget a device and its queue.

    ⚠️ A bridge still running will introduce itself again on its next poll, as a
    new, unadopted row — which is the point: this is how you un-adopt something,
    not how you make it go away.
    """
    device = await _load_device(db, device_id)
    await db.delete(device)
    await db.commit()


# ── Jobs ─────────────────────────────────────────────────────────────────────


@router.post("/label-jobs", response_model=list[LabelJobOut], status_code=201)
async def create_jobs(
    body: LabelJobCreate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.LABEL_JOBS_CREATE),
) -> list[LabelJobOut]:
    """Draw a label for each spool and queue it for this device."""
    await _require_subsystem(db)

    device = await _load_device(db, body.device_id)
    if not device.enabled:
        raise HTTPException(
            409,
            f"'{device.name or device.installation_id}' has not been adopted — enable it before printing to it",
        )

    spec = await resolve_template(db, device, body.template_id)

    deeplink_base = await _resolve_deeplink_base(request, db)
    naming = (await get_setting(db, "spool_display_template") or "").strip()
    contexts = await build_contexts(db, body.spools, deeplink_base=deeplink_base, naming_template=naming)

    jobs, warnings = await enqueue_jobs(
        db,
        device=device,
        spec=spec,
        contexts=contexts,
        spool_ids=[entry.id for entry in body.spools],
        template_id=body.template_id,
        copies=body.copies,
        user_id=user.id if user else None,
    )
    if warnings:
        response.headers["X-Label-Warnings"] = " | ".join(warnings)
        response.headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
    return [_job_out(job) for job in jobs]


@router.post("/label-jobs/preview")
async def preview_job(
    body: LabelJobPreview,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_JOBS_CREATE),
) -> StreamingResponse:
    """The same picture the device would get, without queueing anything.

    Rendered by the same code at the same resolution — a preview drawn any other
    way would be a second implementation of the label, and it would disagree.
    """
    await _require_subsystem(db)

    device = await _load_device(db, body.device_id)
    spec = await resolve_template(db, device, body.template_id)

    deeplink_base = await _resolve_deeplink_base(request, db)
    naming = (await get_setting(db, "spool_display_template") or "").strip()
    contexts = await build_contexts(db, [body.spool], deeplink_base=deeplink_base, naming_template=naming)

    png, warnings = render_job_png(spec, contexts[0])
    headers = {"Content-Length": str(len(png)), "Cache-Control": "no-store"}
    if warnings:
        headers["X-Label-Warnings"] = " | ".join(warnings)
        headers["Access-Control-Expose-Headers"] = "X-Label-Warnings"
    return StreamingResponse(io.BytesIO(png), media_type="image/png", headers=headers)


@router.get("/label-jobs", response_model=list[LabelJobOut])
async def list_jobs(
    device_id: int | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_READ),
) -> list[LabelJobOut]:
    """What is queued, printing, done or broken. Newest first."""
    query = select(LabelJob).order_by(LabelJob.id.desc()).limit(max(1, min(limit, 200)))
    if device_id is not None:
        query = query.where(LabelJob.device_id == device_id)
    return [_job_out(job) for job in (await db.execute(query)).scalars().all()]


@router.delete("/label-jobs/{job_id}", status_code=204, response_model=None)
async def cancel_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_JOBS_CREATE),
) -> None:
    """Take a job out of the queue.

    ⚠️ Only while it is still queued. Once a device has claimed it the paper is
    already moving, and a row deleted from under a bridge that is about to
    report on it turns one wasted label into a confusing error.
    """
    job = await db.get(LabelJob, job_id)
    if job is None:
        raise HTTPException(404, f"Label job {job_id} not found")
    if job.status != "queued":
        raise HTTPException(409, f"job {job_id} is already {job.status} and can no longer be cancelled")
    await db.delete(job)
    await db.commit()


# ── The cassette catalogue ───────────────────────────────────────────────────


@router.get("/label-cassettes", response_model=list[LabelCassetteOut])
async def list_cassettes(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_READ),
) -> list[LabelCassetteOut]:
    rows = (await db.execute(select(LabelCassette).order_by(LabelCassette.barcode))).scalars().all()
    return [
        LabelCassetteOut(id=row.id, barcode=row.barcode, width_mm=row.width_mm, height_mm=row.height_mm, name=row.name)
        for row in rows
    ]


@router.put("/label-cassettes/{barcode}", response_model=LabelCassetteOut)
async def teach_cassette(
    barcode: str,
    body: LabelCassetteIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_MANAGE),
) -> LabelCassetteOut:
    """Say how big the stock with this barcode is.

    ⚠️ **Taught, never fetched.** A self-hosted install does not quietly send
    consumable identifiers to a vendor's cloud to find this out. The person
    holding the cassette can read the box.

    Teaching a barcode does not retro-fix devices that already reported it —
    they resolve it on their next poll, which is seconds away.
    """
    row = (await db.execute(select(LabelCassette).where(LabelCassette.barcode == barcode))).scalar_one_or_none()
    if row is None:
        row = LabelCassette(barcode=barcode)
        db.add(row)
    row.width_mm = body.width_mm
    row.height_mm = body.height_mm
    row.name = (body.name or "").strip() or None
    await db.commit()
    await db.refresh(row)
    return LabelCassetteOut(
        id=row.id, barcode=row.barcode, width_mm=row.width_mm, height_mm=row.height_mm, name=row.name
    )


@router.delete("/label-cassettes/{barcode}", status_code=204, response_model=None)
async def forget_cassette(
    barcode: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_MANAGE),
) -> None:
    row = (await db.execute(select(LabelCassette).where(LabelCassette.barcode == barcode))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"No cassette taught for barcode {barcode}")
    await db.delete(row)
    await db.commit()


# ── The poll ─────────────────────────────────────────────────────────────────


@router.post("/label-devices/poll", response_model=None)
async def poll(
    report: LabelDeviceReport,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_POLL),
) -> LabelJobHandout | Response:
    """One request carries the device's state up and at most one job down.

    ⚠️ **The server sets the cadence**, through ``Retry-After`` on every answer
    — including the refusals. An administrator can slow a chatty bridge down
    from where they already are instead of walking to the machine, and a bridge
    that has just been handed work is told to come straight back, so a batch of
    ten labels drains at printer speed rather than one per poll interval.

    There is no long poll. Holding a request open for a desktop that may be
    asleep buys latency at the cost of a connection per device, forever.
    """
    enabled = (await get_setting(db, "device_labels_enabled") or "").lower() == "true"
    if not enabled:
        return Response(
            status_code=409,
            content='{"detail":"device_labels_disabled"}',
            media_type="application/json",
            headers={"Retry-After": str(DISABLED_BACKOFF_SECONDS)},
        )

    barcode = report.cassette.barcode if report.cassette else None
    cassette = await resolve_cassette(db, barcode)
    device = await upsert_device(db, report, cassette=cassette)

    # ⚠️ A device nobody adopted still gets a 204 rather than a 403. "Alive and
    # waiting for approval" has to be distinguishable from "gone", and the way
    # it stays distinguishable is that the poll keeps succeeding.
    if not device.enabled:
        response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
        return Response(status_code=204, headers={"Retry-After": str(POLL_INTERVAL_SECONDS)})

    # ⚠️ 0 means the printer says it is out of paper; None means it did not say.
    # Paper is a ten-second fix, so the job waits rather than failing — but a
    # device that reports nothing must not be starved by our own caution.
    if report.paper_state == 0 or not report.printer_reachable:
        return Response(status_code=204, headers={"Retry-After": str(POLL_INTERVAL_SECONDS)})

    job = await claim_next_job(db, device.id)
    if job is None:
        return Response(status_code=204, headers={"Retry-After": str(POLL_INTERVAL_SECONDS)})

    response.headers["Retry-After"] = "0"
    return LabelJobHandout(
        job_id=job.id,
        image_png=base64.b64encode(job.image_png).decode("ascii"),
        width_px=round(job.width_mm * DOTS_PER_MM),
        height_px=round(job.height_mm * DOTS_PER_MM),
        width_mm=job.width_mm,
        height_mm=job.height_mm,
        copies=job.copies,
        density=device.density,
    )


@router.post("/label-devices/jobs/{job_id}/result", status_code=204, response_model=None)
async def report_result(
    job_id: int,
    body: LabelJobResult,
    installation_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.LABEL_DEVICES_POLL),
) -> None:
    """What happened to a claimed job.

    ⚠️ Resolved by device **and** id together, so a bridge asking about a job
    that belongs to another device gets a 404 rather than a permission error —
    the latter would confirm that the job exists, which is more than the caller
    is entitled to know.
    """
    device = (
        await db.execute(select(LabelDevice).where(LabelDevice.installation_id == installation_id))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(404, f"Label job {job_id} not found")

    job = (
        await db.execute(select(LabelJob).where(LabelJob.id == job_id, LabelJob.device_id == device.id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, f"Label job {job_id} not found")

    # ⚠️ A report on a job that is no longer claimed is accepted quietly rather
    # than refused. The sweeper may have requeued it while the bridge was
    # printing it perfectly well, and answering 409 to a device that just did
    # the work would leave it retrying something that already came out.
    if body.ok:
        job.status = "printed"
        job.error = None
    else:
        job.status = "failed"
        job.error = (body.error or "").strip() or "the device did not say why"
    await db.commit()
