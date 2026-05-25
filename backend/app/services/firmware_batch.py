"""Bulk firmware orchestrator.

Modeled on ``background_dispatch`` but SEPARATE from the print-dispatch layer
(firmware != prints — the single-dispatch invariant stays intact). Groups
targets by model, downloads each model's firmware once via the shared store,
fans out FTP-to-SD under a concurrency cap, and (Phase 2) applies remotely where
the model's profile allows it. Skips printing printers; continues on per-printer
failure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.firmware import FirmwareBatchItem, FirmwareBatchRun
from backend.app.models.printer import Printer
from backend.app.services import firmware_store
from backend.app.services.bambu_ftp import get_ftp_retry_settings, upload_file_async, with_ftp_retry
from backend.app.services.firmware_profiles import get_firmware_profile
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2


@dataclass
class BatchTarget:
    printer_id: int
    model: str
    version: str
    from_version: str | None = None


def _is_printing(printer_id: int) -> bool:
    """True if the printer is mid-job (RUNNING/PAUSE) — never FTP to its SD then."""
    client = printer_manager.get_client(printer_id)
    if not client or not client.state:
        return False
    return client.state.state in ("RUNNING", "PAUSE")


async def _ftp_upload(item, sf) -> None:
    """FTP the firmware .bin to the printer's SD root. Raises on failure."""
    async with async_session() as db:
        printer = (await db.execute(select(Printer).where(Printer.id == item.printer_id))).scalar_one()
        ip, code, model = printer.ip_address, printer.access_code, printer.model or "Unknown"
    remote_path = f"/{sf.filename}"
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
    if ftp_retry_enabled:
        ok = await with_ftp_retry(
            upload_file_async,
            ip,
            code,
            sf.path,
            remote_path,
            socket_timeout=ftp_timeout,
            printer_model=model,
            max_retries=ftp_retry_count,
            retry_delay=ftp_retry_delay,
            operation_name=f"Bulk firmware upload to printer {item.printer_id}",
        )
    else:
        ok = await upload_file_async(ip, code, sf.path, remote_path, socket_timeout=ftp_timeout, printer_model=model)
    if not ok:
        raise RuntimeError("FTP upload returned failure")


async def _broadcast(run_id: int, printer_id: int, status: str, message: str = "", percent: int = 0) -> None:
    await ws_manager.broadcast(
        {
            "type": "firmware_batch_progress",
            "run_id": run_id,
            "printer_id": printer_id,
            "status": status,
            "message": message,
            "percent": percent,
        }
    )


async def _set_item(item_id: int, **fields) -> None:
    async with async_session() as db:
        item = (await db.execute(select(FirmwareBatchItem).where(FirmwareBatchItem.id == item_id))).scalar_one()
        for k, v in fields.items():
            setattr(item, k, v)
        await db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _process_one(run_id: int, item, sf, sem: asyncio.Semaphore) -> str:
    async with sem:
        await _set_item(item.id, started_at=_now())
        if _is_printing(item.printer_id):
            await _set_item(item.id, status="skipped", message="printing", finished_at=_now())
            await _broadcast(run_id, item.printer_id, "skipped", "printing")
            return "skipped"
        try:
            await _set_item(item.id, status="uploading")
            await _broadcast(run_id, item.printer_id, "uploading", percent=10)
            await _ftp_upload(item, sf)
            profile = get_firmware_profile(item.model)
            if profile.remote_apply:
                from backend.app.services.firmware_apply import apply_remote

                await _set_item(item.id, status="applying", action="remote_apply")
                await _broadcast(run_id, item.printer_id, "applying", percent=80)
                await apply_remote(item.printer_id, item.model, item.to_version, profile)
                await _set_item(item.id, status="applied", finished_at=_now())
                await _broadcast(run_id, item.printer_id, "applied", percent=100)
                return "applied"
            await _set_item(
                item.id,
                status="uploaded",
                message=profile.manual_apply_instruction_key,
                finished_at=_now(),
            )
            await _broadcast(run_id, item.printer_id, "uploaded", profile.manual_apply_instruction_key, 100)
            return "uploaded"
        except Exception as exc:  # continue-on-failure
            logger.error("Bulk firmware item %s (printer %s) failed: %s", item.id, item.printer_id, exc)
            await _set_item(item.id, status="failed", error=str(exc), finished_at=_now())
            await _broadcast(run_id, item.printer_id, "failed", str(exc))
            return "failed"


async def _run_targets(
    run_id: int, targets: list[BatchTarget], items_by_printer: dict, concurrency: int
) -> dict[str, str]:
    """Group by model, download each model's firmware once, fan out under a cap.

    ``items_by_printer`` maps printer_id → the FirmwareBatchItem (DB row) for this
    run. Returns ``{str(printer_id): final_status}`` (str keys for easy json/assert).
    """
    sem = asyncio.Semaphore(concurrency)
    by_model: dict[tuple[str, str], list[BatchTarget]] = {}
    for t in targets:
        by_model.setdefault((t.model, t.version), []).append(t)

    outcome: dict[str, str] = {}
    for (model, version), group in by_model.items():
        sf = await firmware_store.get_or_download(model, version)
        if sf is None:
            for t in group:  # whole model group fails; other models continue
                it = items_by_printer[t.printer_id]
                await _set_item(it.id, status="failed", error="firmware download/cache unavailable", finished_at=_now())
                await _broadcast(run_id, t.printer_id, "failed", "firmware unavailable")
                outcome[str(t.printer_id)] = "failed"
            continue
        results = await asyncio.gather(*[_process_one(run_id, items_by_printer[t.printer_id], sf, sem) for t in group])
        for t, r in zip(group, results, strict=True):
            outcome[str(t.printer_id)] = r
    return outcome


class FirmwareBatchService:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def start_batch(self, targets: list[BatchTarget], actor_id: int | None) -> int:
        """Create the run + item rows, kick off the async run, return run_id."""
        async with async_session() as db:
            run = FirmwareBatchRun(created_by_id=actor_id, status="running", total=len(targets))
            db.add(run)
            await db.flush()
            for t in targets:
                profile = get_firmware_profile(t.model)
                db.add(
                    FirmwareBatchItem(
                        run_id=run.id,
                        printer_id=t.printer_id,
                        model=t.model,
                        from_version=t.from_version,
                        to_version=t.version,
                        action="remote_apply" if profile.remote_apply else "download_only",
                        status="pending",
                    )
                )
            await db.commit()
            run_id = run.id

        concurrency = await _get_concurrency()
        task = asyncio.create_task(self._run_and_finalize(run_id, targets, concurrency))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _run_and_finalize(self, run_id: int, targets: list[BatchTarget], concurrency: int) -> None:
        async with async_session() as db:
            items = (
                (await db.execute(select(FirmwareBatchItem).where(FirmwareBatchItem.run_id == run_id))).scalars().all()
            )
        items_by_printer = {it.printer_id: it for it in items}

        outcome = await _run_targets(run_id, targets, items_by_printer, concurrency)
        succeeded = sum(1 for v in outcome.values() if v in ("uploaded", "applied"))
        skipped = sum(1 for v in outcome.values() if v == "skipped")
        failed = sum(1 for v in outcome.values() if v == "failed")
        async with async_session() as db:
            run = (await db.execute(select(FirmwareBatchRun).where(FirmwareBatchRun.id == run_id))).scalar_one()
            run.succeeded, run.skipped, run.failed = succeeded, skipped, failed
            run.status = "completed"
            await db.commit()

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()


async def _get_concurrency() -> int:
    """Read firmware_batch_concurrency from settings; default 2."""
    from backend.app.api.routes.settings import get_setting

    async with async_session() as db:
        raw = await get_setting(db, "firmware_batch_concurrency")
    try:
        return max(1, int(raw)) if raw else DEFAULT_CONCURRENCY
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY


firmware_batch_service = FirmwareBatchService()
