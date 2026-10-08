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

import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.db_dialect import insert_ignoring_conflicts
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.models.product import ProductPlate
from backend.app.services import part_images
from backend.app.services.part_render_protocol import (
    GC_GRACE_SECONDS,
    MASTER_SIZE,
    MAX_ATTEMPTS,
    METHODS,
    MISSING_REASONS,
    RENDERED,
    RENDERER_VERSION,
    RETRY_BASE_SECONDS,
    SMALL_SIZE,
)
from backend.app.services.part_render_types import AttemptResult, RenderTask, SourceRef
from backend.app.services.preview_artifacts import disk, validate
from backend.app.services.product_facets import id_chunks
from backend.app.services.worker_staging import cleanup_owned

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
    result = await db.execute(insert_ignoring_conflicts(db, PlateRender.__table__, values, list(_KEY)))
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


_RESULT_NAME = re.compile(r"[0-9a-f]{32}\Z")
_VALIDATE_SECONDS = 120


class InvalidResult(ValueError):
    """The files do not answer their own manifest -- the attempt counts as invalid_output."""


def result_path(root: Path, file_sha256: str, renderer_version: int, plate_index: int, name: str) -> Path:
    """``part-renders/<sha[:2]>/<sha>/v<version>/p<plate>/<name>`` (spec §8.2): the only layout."""
    return root / file_sha256[:2] / file_sha256 / f"v{renderer_version}" / f"p{plate_index}" / name


def _verify(files: Path) -> tuple[dict, bytes]:
    """Spec §9.3 step 1: every file is what the manifest says, every PNG decodes at its size."""
    deadline = time.monotonic_ns() + _VALIDATE_SECONDS * 10**9
    raw = (files / "manifest.json").read_bytes()
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise InvalidResult("manifest is not JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("renderer") != RENDERER_VERSION:
        raise InvalidResult("manifest of another renderer")
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise InvalidResult("manifest has no object list")
    seen: set[int] = set()
    expected = {"manifest.json"}
    for entry in objects:
        oid, method = entry.get("identify_id"), entry.get("method")
        if type(oid) is not int or oid in seen or method not in METHODS:
            raise InvalidResult(f"malformed instance {oid!r}")
        seen.add(oid)
        declared = entry.get("files") or {}
        if method not in RENDERED:
            if declared or entry.get("reason") not in MISSING_REASONS:
                raise InvalidResult(f"{oid}: {method} carries files or no reason")
            continue
        if set(declared) != {"lg", "sm"}:
            raise InvalidResult(f"{oid}: not exactly a master and a small size")
        master = (entry.get("width"), entry.get("height"))
        if method in ("toolpath", "model") and master != (MASTER_SIZE, MASTER_SIZE):
            raise InvalidResult(f"{oid}: a {method} master is {master}")
        for size_name, size in (("lg", master), ("sm", (SMALL_SIZE, SMALL_SIZE))):
            path = files / f"{oid}.{size_name}.png"  # SEC-PATH-OK: oid is an int
            data = path.read_bytes()
            meta = declared[size_name]
            if len(data) != meta.get("bytes") or hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                raise InvalidResult(f"{path.name} does not match the manifest")
            try:
                validate(path, "png", deadline)
                with Image.open(path) as img:
                    if img.size != size:
                        raise InvalidResult(f"{path.name} is {img.size}, not {size}")
            except InvalidResult:
                raise
            except Exception as exc:
                raise InvalidResult(f"{path.name} is not a valid PNG") from exc
            expected.add(path.name)
    if {p.name for p in files.iterdir()} != expected:
        raise InvalidResult("files beside the manifest")
    return manifest, raw


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a directory for fsync; NTFS journals the rename
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_result_dir(files: Path, parent: Path, name: str) -> Path:
    """A new attempt directory: written under ``<name>.part``, fsynced, renamed whole (spec §9.3 step 2)."""
    parent.mkdir(parents=True, exist_ok=True)
    part = parent / f"{name}.part"
    part.mkdir()
    for source in files.iterdir():
        with source.open("rb") as src, (part / source.name).open("xb") as dst:  # SEC-PATH-OK: names checked by _verify
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    _fsync_dir(part)
    final = parent / name
    os.replace(part, final)  # the name is new, so the rename never meets a non-empty directory
    _fsync_dir(parent)
    return final


async def publish(
    session_factory, root: Path, task: RenderTask, result: AttemptResult, generation: str, *, reason: str | None
) -> str:
    """Spec §9.3: ``published`` (committed), ``stale`` (the key or the generation moved; the new directory is gone)
    or ``unknown`` (the commit's outcome is unknown; nothing is deleted, the start-up reconciliation decides)."""
    manifest, raw = await disk(_verify, result.files)
    name = uuid4().hex
    parent = result_path(root, task.file_sha256, task.renderer_version, task.plate_index, "")
    directory = await disk(_write_result_dir, result.files, parent, name)
    if generation != _live_generation:
        await disk(cleanup_owned, directory)
        return "stale"
    now = utcnow()
    try:
        async with session_factory() as db:
            previous = (await db.execute(select(PlateRender.result_dir).where(*_full_key(task)))).scalar_one_or_none()
            changed = await db.execute(
                update(PlateRender)
                .where(*_full_key(task))
                .values(
                    status="ready",
                    phase="render",
                    reason=reason,
                    result_dir=name,
                    manifest_sha256=hashlib.sha256(raw).hexdigest(),
                    runtime_version=result.runtime_version,
                    attempts=0,
                    last_error=None,
                    finished_at=now,
                    elapsed_ms=result.elapsed_ms,
                )
            )
            if changed.rowcount != 1 or generation != _live_generation:
                await db.rollback()
                await disk(cleanup_owned, directory)
                return "stale"
            await db.execute(delete(PlateRenderObject).where(PlateRenderObject.render_id == task.render_id))
            await db.execute(
                insert(PlateRenderObject),
                [
                    {
                        "render_id": task.render_id,
                        "identify_id": entry["identify_id"],
                        "method": entry["method"],
                        "reason": entry.get("reason"),
                        "width": entry.get("width"),
                        "height": entry.get("height"),
                        "tools": list(entry.get("tools") or []),
                    }
                    for entry in manifest["objects"]
                ],
            )
            await db.commit()
    except Exception:
        return "unknown"  # possibly committed: the directory stays, the reconciliation decides by the database
    if previous and _RESULT_NAME.fullmatch(previous) and previous != name:
        await disk(
            cleanup_owned, result_path(root, task.file_sha256, task.renderer_version, task.plate_index, previous)
        )
    part_images.mark_changed([(task.file_sha256, task.plate_index)])
    return "published"


logger = logging.getLogger(__name__)

_HEX2 = re.compile(r"[0-9a-f]{2}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_DIR = re.compile(r"v(\d+)\Z")
_PLATE_DIR = re.compile(r"p(\d+)\Z")
_PARTIAL = re.compile(r"[0-9a-f]{32}\.part\Z")


@dataclass
class ReconcileReport:
    kept: int = 0
    cleared: int = 0
    removed: int = 0
    foreign: int = 0


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _scan(root: Path) -> tuple[dict[tuple[str, int, int, str], Path], list[Path], list[Path]]:
    """Attempt directories by (sha, version, plate, name), ``*.part`` directories, and entries outside the
    layout. Read-only, and run with no database session open (m148)."""
    found: dict[tuple[str, int, int, str], Path] = {}
    partial: list[Path] = []
    foreign: list[Path] = []
    if not _real_dir(root):
        return found, partial, foreign
    for prefix in root.iterdir():
        if not (_real_dir(prefix) and _HEX2.fullmatch(prefix.name)):
            foreign.append(prefix)
            continue
        for sha_dir in prefix.iterdir():
            if not (_real_dir(sha_dir) and _SHA.fullmatch(sha_dir.name) and sha_dir.name[:2] == prefix.name):
                foreign.append(sha_dir)
                continue
            for version_dir in sha_dir.iterdir():
                version = _VERSION_DIR.fullmatch(version_dir.name)
                if not (_real_dir(version_dir) and version):
                    foreign.append(version_dir)
                    continue
                for plate_dir in version_dir.iterdir():
                    plate = _PLATE_DIR.fullmatch(plate_dir.name)
                    if not (_real_dir(plate_dir) and plate):
                        foreign.append(plate_dir)
                        continue
                    for entry in plate_dir.iterdir():
                        if _real_dir(entry) and _RESULT_NAME.fullmatch(entry.name):
                            found[(sha_dir.name, int(version.group(1)), int(plate.group(1)), entry.name)] = entry
                        elif _real_dir(entry) and _PARTIAL.fullmatch(entry.name):
                            partial.append(entry)
                        else:
                            foreign.append(entry)
    return found, partial, foreign


def _complete(directory: Path | None, manifest_sha256: str | None) -> bool:
    """Spec §8.4 row 1: the manifest is the recorded one and every file it names is there at its size."""
    if directory is None or not manifest_sha256:
        return False
    try:
        raw = (directory / "manifest.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            return False
        for entry in json.loads(raw)["objects"]:
            for size_name, meta in (entry.get("files") or {}).items():
                path = directory / f"{int(entry['identify_id'])}.{size_name}.png"  # SEC-PATH-OK: int + fixed names
                if path.stat().st_size != meta["bytes"]:
                    return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


async def reconcile(session_factory, root: Path) -> ReconcileReport:
    """Spec §8.4, at every start before admission: no attempt is alive, so a directory without an owning row
    has no owner. Deletes only inside ``root`` and only by the layout's names."""
    report = ReconcileReport()
    found, partial, foreign = await disk(_scan, root)
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(
                    PlateRender.id,
                    PlateRender.file_sha256,
                    PlateRender.renderer_version,
                    PlateRender.plate_index,
                    PlateRender.result_dir,
                    PlateRender.manifest_sha256,
                ).where(PlateRender.result_dir.is_not(None))
            )
        ).all()
    for row in rows:
        directory = found.get((row.file_sha256, row.renderer_version, row.plate_index, row.result_dir))
        if await disk(_complete, directory, row.manifest_sha256):
            report.kept += 1
            continue
        async with session_factory() as db:
            await db.execute(delete(PlateRenderObject).where(PlateRenderObject.render_id == row.id))
            await db.execute(
                update(PlateRender)
                .where(PlateRender.id == row.id, PlateRender.result_dir == row.result_dir)
                .values(result_dir=None, manifest_sha256=None)
            )
            await db.execute(
                update(PlateRender)
                .where(PlateRender.id == row.id, PlateRender.status == "ready")
                .values(status="pending", phase="render", reason=None, attempts=0, priority=0, next_attempt_at=utcnow())
            )
            await db.commit()
        report.cleared += 1
        if directory is not None:
            await disk(cleanup_owned, directory)
        part_images.mark_changed([(row.file_sha256, row.plate_index)])
    named = {(r.file_sha256, r.renderer_version, r.plate_index, r.result_dir) for r in rows}
    for key, directory in found.items():
        if key not in named:  # a named but broken directory went with its row above
            await disk(cleanup_owned, directory)
            report.removed += 1
    for directory in partial:
        await disk(cleanup_owned, directory)
        report.removed += 1
    for entry in foreign:
        logger.warning("Part render root holds an entry outside its layout, left alone: %s", entry.name)
    report.foreign = len(foreign)
    logger.info(
        "Part render reconcile kept=%d cleared=%d removed=%d foreign=%d",
        report.kept,
        report.cleared,
        report.removed,
        report.foreign,
    )
    return report


def _linked_at_all(sha256_column):
    """A file in the trash counts: restored, its pictures are there at once (spec §9.5)."""
    return (
        select(LibraryFile.id)
        .join(ProductPlate, ProductPlate.library_file_id == LibraryFile.id)
        .where(LibraryFile.file_hash == sha256_column)
        .exists()
    )


def _orphan():
    """No file links the hash any more, or the row is of another renderer version (spec §9.5)."""
    return or_(~_linked_at_all(PlateRender.file_sha256), PlateRender.renderer_version != RENDERER_VERSION)


async def gc_due(session_factory, now: datetime) -> list:
    """Start the grace of new orphans, end it for rows linked again, and list the rows past it (plan E3, R7)."""
    async with session_factory() as db:
        await db.execute(
            update(PlateRender)
            .where(_orphan(), PlateRender.orphaned_at.is_(None))
            .values(orphaned_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(PlateRender)
            .where(~_orphan(), PlateRender.orphaned_at.is_not(None))
            .values(orphaned_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (
            await db.execute(
                select(
                    PlateRender.id,
                    PlateRender.file_sha256,
                    PlateRender.renderer_version,
                    PlateRender.plate_index,
                    PlateRender.result_dir,
                ).where(_orphan(), PlateRender.orphaned_at <= now - timedelta(seconds=GC_GRACE_SECONDS))
            )
        ).all()


async def gc_remove(session_factory, root: Path, due: list, now: datetime) -> int:
    """Delete what is STILL an orphan past its grace -- the condition again, inside the DELETE -- and the
    instance rows and directories of exactly the rows that went, read back through RETURNING in the same
    transaction. A hash linked again between the choice and the delete keeps its row, its instances and
    its directory (consilium E3-R3)."""
    due_before = now - timedelta(seconds=GC_GRACE_SECONDS)
    removed = 0
    for start in range(0, len(due), 500):
        batch = {row.id: row for row in due[start : start + 500]}
        async with session_factory() as db:
            gone = set(
                (
                    await db.execute(
                        delete(PlateRender)
                        .where(PlateRender.id.in_(list(batch)), _orphan(), PlateRender.orphaned_at <= due_before)
                        .returning(PlateRender.id)
                        .execution_options(synchronize_session=False)
                    )
                ).scalars()
            )
            if gone:  # PostgreSQL's CASCADE has taken them already; SQLite runs no FK actions
                await db.execute(delete(PlateRenderObject).where(PlateRenderObject.render_id.in_(sorted(gone))))
            await db.commit()
        removed += len(gone)
        for render_id in sorted(gone):
            row = batch[render_id]
            if row.result_dir and _RESULT_NAME.fullmatch(row.result_dir):
                await disk(
                    cleanup_owned,
                    result_path(root, row.file_sha256, row.renderer_version, row.plate_index, row.result_dir),
                )
    return removed


async def gc(session_factory, root: Path, now: datetime) -> int:
    """Spec §9.5, only when no attempt is alive (the scheduler's idle pass)."""
    return await gc_remove(session_factory, root, await gc_due(session_factory, now), now)
