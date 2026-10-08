"""The only writer of plate_renders, plate_render_objects and part-renders/ (spec §8, §9; plan E3, tasks 20-22).

Rows are keyed by content: file_sha256, plate, renderer version. The queue picks a pending row whose hash
still has a linked file outside the trash; a hash whose files are all in the trash is skipped, not
parked, so the queue never starves (spec §9.2). Every transition the scheduler makes is a CAS on the
full key and the live generation (spec §9.3, §9.6), in its own short transaction, and ends in
part_images.mark_changed. ensure_for_files and rerender_for_product run in the caller's transaction and
never commit, like product_facets.refresh. Main never touches a source file: SourceRef is composed from
the row alone. SQLite runs no FK actions, so instance rows are deleted here, in code.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.plate_render import PlateRender
from backend.app.models.product import ProductPlate
from backend.app.services import part_images
from backend.app.services.part_render_protocol import MAX_ATTEMPTS, RENDERER_VERSION, RETRY_BASE_SECONDS
from backend.app.services.part_render_types import RenderTask, SourceRef
from backend.app.services.product_facets import id_chunks

_KEY = ["file_sha256", "plate_index", "renderer_version"]
_live_generation: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def set_live_generation(generation: str | None) -> None:
    """The scheduler's generation; a transition made for any other writes nothing (spec §9.6)."""
    global _live_generation
    _live_generation = generation


def _has_live_linked_file(sha256_column):
    return (
        select(LibraryFile.id)
        .join(ProductPlate, ProductPlate.library_file_id == LibraryFile.id)
        .where(LibraryFile.file_hash == sha256_column, LibraryFile.deleted_at.is_(None))
        .exists()
    )


def _sliced_linked_plates():
    return (
        select(LibraryFile.file_hash, ProductPlate.plate_index)
        .join(ProductPlate, ProductPlate.library_file_id == LibraryFile.id)
        .where(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.file_hash.is_not(None),
            func.length(LibraryFile.file_hash) == 64,
            LibraryFile.file_type == "gcode",  # .gcode.3mf and raw .gcode: the sliced files (spec §9.1)
        )
        .distinct()
    )


async def _insert_pending(db: AsyncSession, keys: set[tuple[str, int]], priority: int) -> int:
    if not keys:
        return 0
    now = utcnow()
    values = [
        {
            "file_sha256": sha,
            "plate_index": plate,
            "renderer_version": RENDERER_VERSION,
            "status": "pending",
            "phase": "render",
            "priority": priority,
            "attempts": 0,
            "next_attempt_at": now,
            "requested_at": now,
        }
        for sha, plate in sorted(keys)
    ]
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    result = await db.execute(insert(PlateRender.__table__).values(values).on_conflict_do_nothing(index_elements=_KEY))
    return result.rowcount or 0


async def ensure_for_files(db: AsyncSession, library_file_ids: Iterable[int], *, priority: int = 1) -> int:
    """A pending row per plate of every linked, sliced file outside the trash (spec §9.1). Never commits."""
    total = 0
    for chunk in id_chunks(library_file_ids):
        found = (await db.execute(_sliced_linked_plates().where(LibraryFile.id.in_(chunk)))).all()
        total += await _insert_pending(db, {(sha, plate) for sha, plate in found}, priority)
    return total


async def backfill(session_factory) -> int:
    """One pass over every linked, sliced file at backfill priority (spec §9.1); its own short transactions."""
    async with session_factory() as db:
        found = sorted({(sha, plate) for sha, plate in (await db.execute(_sliced_linked_plates())).all()})
    total = 0
    for start in range(0, len(found), 500):
        async with session_factory() as db:
            total += await _insert_pending(db, set(found[start : start + 500]), 0)
            await db.commit()
    return total


async def _source_for(db: AsyncSession, file: LibraryFile) -> SourceRef:
    """From the row alone: main never stats, resolves or opens the path (plan E3, Global Constraints)."""
    path = Path(file.file_path) if file.is_external else Path(settings.base_dir) / file.file_path
    root = None
    if file.is_external and file.folder_id is not None:
        root = (
            await db.execute(select(LibraryFolder.external_path).where(LibraryFolder.id == file.folder_id))
        ).scalar_one_or_none()
    if not root:
        root = str(path.parent) if file.is_external else str(settings.library_dir)
    kind = "3mf" if file.filename.lower().endswith(".3mf") else "gcode"
    return SourceRef(library_file_id=file.id, path=str(path), root=root, kind=kind, size=file.file_size)


async def next_task(db: AsyncSession, now: datetime) -> RenderTask | None:
    row = (
        await db.execute(
            select(PlateRender)
            .where(
                PlateRender.status == "pending",
                PlateRender.renderer_version == RENDERER_VERSION,
                PlateRender.next_attempt_at <= now,
                _has_live_linked_file(PlateRender.file_sha256),
            )
            .order_by(PlateRender.priority.desc(), PlateRender.requested_at, PlateRender.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    file = (
        await db.execute(
            select(LibraryFile)
            .join(ProductPlate, ProductPlate.library_file_id == LibraryFile.id)
            .where(LibraryFile.file_hash == row.file_sha256, LibraryFile.deleted_at.is_(None))
            .order_by(LibraryFile.id)
            .limit(1)
        )
    ).scalar_one()
    return RenderTask(
        render_id=row.id,
        file_sha256=row.file_sha256,
        plate_index=row.plate_index,
        renderer_version=row.renderer_version,
        phase=row.phase,
        reason=row.reason,
        attempts=row.attempts,
        priority=row.priority,
        has_result=row.result_dir is not None,
        source=await _source_for(db, file),
    )


async def queue_counts(db: AsyncSession) -> dict:
    counted = dict(
        (
            await db.execute(
                select(PlateRender.status, func.count())
                .where(PlateRender.renderer_version == RENDERER_VERSION, PlateRender.status.in_(("pending", "failed")))
                .group_by(PlateRender.status)
            )
        ).all()
    )
    return {"pending": counted.get("pending", 0), "failed": counted.get("failed", 0)}


def _full_key(task: RenderTask):
    return (
        PlateRender.id == task.render_id,
        PlateRender.file_sha256 == task.file_sha256,
        PlateRender.plate_index == task.plate_index,
        PlateRender.renderer_version == task.renderer_version,
        PlateRender.status == "pending",
    )


async def _transition(session_factory, task: RenderTask, generation: str, values: dict) -> bool:
    """CAS on the full key and the live generation (spec §9.3); False when nothing was written."""
    if generation != _live_generation:
        return False
    async with session_factory() as db:
        result = await db.execute(update(PlateRender).where(*_full_key(task)).values(**values))
        if result.rowcount != 1 or generation != _live_generation:
            await db.rollback()
            return False
        await db.commit()
    part_images.mark_changed([(task.file_sha256, task.plate_index)])
    return True


async def record_attempt_failure(
    session_factory, task: RenderTask, reason: str, generation: str, now: datetime
) -> bool:
    """A transient failure in phase render (spec §5.3; plan 5.1 transition table)."""
    attempts = task.attempts + 1
    if attempts < MAX_ATTEMPTS:
        values = {
            "attempts": attempts,
            "next_attempt_at": now + timedelta(seconds=RETRY_BASE_SECONDS * 2**task.attempts),
            "last_error": reason,
        }
    elif task.has_result:
        values = {
            "status": "ready",
            "reason": "rerender_failed",
            "attempts": 0,
            "finished_at": now,
            "last_error": reason,
        }
    else:
        values = {
            "phase": "fallback",
            "reason": "render_failed",
            "attempts": 0,
            "next_attempt_at": now,
            "last_error": reason,
        }
    return await _transition(session_factory, task, generation, values)


async def enter_fallback(session_factory, task: RenderTask, reason: str, generation: str, now: datetime) -> bool:
    """memory_limit, or a platform without an official Node: the fallback phase, the attempt not counted."""
    return await _transition(
        session_factory, task, generation, {"phase": "fallback", "reason": reason, "next_attempt_at": now}
    )


async def mark_terminal(
    session_factory, task: RenderTask, status: str, reason: str, generation: str, now: datetime
) -> bool:
    if status not in ("failed", "unavailable"):
        raise ValueError(f"not a terminal status: {status!r}")
    return await _transition(
        session_factory,
        task,
        generation,
        {"status": status, "reason": reason, "finished_at": now, "last_error": reason},
    )


async def requeue_no_runtime(session_factory) -> int:
    """A runtime appeared: plates finished by the fallback alone go back, their pictures kept (spec §9.1)."""
    async with session_factory() as db:
        result = await db.execute(
            update(PlateRender)
            .where(
                PlateRender.status == "ready",
                PlateRender.reason == "no_runtime",
                PlateRender.renderer_version == RENDERER_VERSION,
            )
            .values(status="pending", phase="render", reason=None, attempts=0, priority=0, next_attempt_at=utcnow())
        )
        await db.commit()
    return result.rowcount or 0


async def rerender_for_product(db: AsyncSession, product_id: int, *, full: bool) -> int:
    """Spec §9.1: this product's failed / unavailable plates back to pending at priority 2; with ``full`` the
    ready ones too, their result_dir kept so the old pictures show until the new ones are published."""
    files = (
        (await db.execute(select(ProductPlate.library_file_id).where(ProductPlate.product_id == product_id)))
        .scalars()
        .all()
    )
    await ensure_for_files(db, files, priority=2)
    keys = set((await db.execute(_sliced_linked_plates().where(ProductPlate.product_id == product_id))).all())
    statuses = ("failed", "unavailable", "ready") if full else ("failed", "unavailable")
    now = utcnow()
    count = 0
    for sha, plate in keys:
        result = await db.execute(
            update(PlateRender)
            .where(
                PlateRender.file_sha256 == sha,
                PlateRender.plate_index == plate,
                PlateRender.renderer_version == RENDERER_VERSION,
                PlateRender.status.in_(statuses),
            )
            .values(
                status="pending",
                phase="render",
                reason=None,
                attempts=0,
                priority=2,
                next_attempt_at=now,
                requested_at=now,
            )
        )
        count += result.rowcount or 0
    return count
