"""Shared source validation at the queue's write boundaries."""

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import update

from backend.app.core.config import settings
from backend.app.i18n import current_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.queue_source import FORMAT_GCODE
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache


async def fail_auto_source(db, item, reason):
    """Only fail an unclaimed item; a concurrent assignment/cancel stays authoritative."""
    await db.execute(
        update(AutoQueueItem)
        .where(AutoQueueItem.id == item.id, AutoQueueItem.status == "pending", AutoQueueItem.cancelled_at.is_(None))
        .values(status="failed", waiting_reason=routing_detail(reason)["message"])
    )


def resolve_source_path(archive=None, library_file=None) -> Path | None:
    source = archive or library_file
    if source is None or not source.file_path:
        return None
    path = Path(source.file_path)
    return (
        path if path.is_absolute() else settings.base_dir / path
    )  # SEC-PATH-OK: persisted server-owned source path; external paths may be absolute.


def routing_detail(code: str, **params) -> dict:
    return {"code": code, "params": params, "message": t(current_language(), "filament_routing", code, **params)}


async def item_source(db, item):
    archive = await db.get(PrintArchive, item.archive_id) if item.archive_id else None
    library = None
    if not item.archive_id and item.library_file_id:
        library = (
            await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
        ).scalar_one_or_none()
    return archive, library


async def read_item_requirements(db, item, cache: PrintRequirementsCache | None = None) -> PrintRequirements:
    archive, library = await item_source(db, item)
    return await (cache or PrintRequirementsCache()).read(
        resolve_source_path(archive, library), item.plate_id, archive_plate_id=archive.plate_index if archive else None
    )


async def require_source_requirements(
    cache: PrintRequirementsCache,
    archive=None,
    library_file=None,
    plate_id: int | None = None,
    *,
    allow_raw_gcode: bool = False,
    product_plate_id: int | None = None,
    source_path: Path | None = None,
    source_format: str | None = None,
) -> PrintRequirements | None:
    """Requirements for a queue writer's source, or a 422 naming what is wrong.

    ``source_path`` / ``source_format`` are the *bytes to read instead of the
    original* — a queue source's staged copy (spec §5 step 4), whose name is a
    random ``.part`` token and so cannot be asked what container it is. The row
    still comes from ``archive`` / ``library_file``: the refusal names that id,
    and an archive still contributes its plate fallback.
    ``services/queue_source_capture.py::staged_requirements`` is the only caller
    that passes them.
    """
    source = archive or library_file
    path = resolve_source_path(archive, library_file) if source_path is None else source_path
    raw_gcode = (
        source_format == FORMAT_GCODE
        if source_format is not None
        else path is not None and path.suffix.lower() == ".gcode"
    )
    # Only a server-identified raw G-code source keeps the existing explicit
    # per-printer workflow. A broken 3MF never acquires this exemption.
    if allow_raw_gcode and raw_gcode:
        return None
    req = await cache.read(path, plate_id, archive_plate_id=archive.plate_index if archive else None)
    if req.status != "ok":
        raise HTTPException(
            422,
            routing_detail(
                req.reason or "source_unreadable",
                source_id=source.id if source else None,
                plate_id=plate_id,
                product_plate_id=product_plate_id,
            ),
        )
    return req
