"""What the queue does when an original is deleted — spec §9 and §10.

Spec: ``60-specs/queue-source-spool-spec.md``. Since m173 a queued job owns a
local copy of the bytes it prints, and §10 spells out the consequence: trashing a
library file, purging an external one, the retention sweeper, the scanner finding
a vanished share and an archive's trash or hard delete all **detach the
navigational link and leave the work standing** — its status, its position, its
order line and its name. Only a job that has no usable copy of its own is
cancelled, visibly and with a reason, exactly as it was before the spool existed.

Three things live here, because they are three faces of one question — *which
queued jobs still need this original?*

* :func:`delete_impact` answers it for a confirmation dialog (§10's "a mixed
  selection must truthfully show which jobs are self-contained").
* :func:`source_trashed` answers it for a soft delete: the original row is still
  there and restorable, so the link stays and only the dependents are cancelled.
* :func:`source_purged` answers it for a hard delete: the dependents are
  cancelled and then the link is cut — in code, because SQLite enforces no
  ``ON DELETE SET NULL`` (this codebase never sets ``PRAGMA foreign_keys = ON``),
  and because leaving a dangling id behind is what m173's rule change was for.

⚠️ **One owner question, asked in one place.** Both tiers count, because
:data:`queue_sources.OWNER_COLUMNS` says both tiers own blobs; and the verdict
per row is :func:`queue_source_descriptor.source_storage_state` — the same pure
function that fills the ``source_storage`` field the queue card reads. Neither is
re-implemented here. A second rule for "is this job self-contained" would be free
to drift from the collector's, and that drift is a file unlinked under a running
print (S5).

⚠️ **Nothing in this module deletes a job row or unlinks a blob.** A reference is
freed by its row going away (``queue_counters.detach_print_queue_refs`` and the
delete routes), and the bytes are released only by the collector, after its grace
window (§9: "не стирати в live completion callback").
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_sources
from backend.app.services.queue_source_descriptor import source_storage_state

logger = logging.getLogger(__name__)

#: The reasons the queue records when a source goes away. Constants because two
#: services write them and one test pins the exact sentence — the prose is the
#: operator's only explanation of a row they did not cancel themselves.
#:
#: ⚠️ English, like every other ``waiting_reason`` a delete path writes. That
#: column carries backend prose the frontend renders as-is; the translated half of
#: a wait is ``waiting_reason_code``, whose values are a closed list read by
#: ``monitor_snapshot`` — and "your source was deleted" is not one of them.
REASON_FILE_DELETED = "Source file deleted"
REASON_ARCHIVE_DELETED = "Source archive deleted"
REASON_FILE_VANISHED = "Source file is no longer in its folder"

#: The one status on either tier that a delete still changes. A ``printing`` row
#: is the printer's race to lose and its fail path catches it; everything terminal
#: is history, and rewriting history would edit the record of what happened.
_PENDING = "pending"

#: What an un-routable auto row becomes. ``failed`` rather than ``cancelled``
#: because that is the status the auto-queue panel shows (it asks for
#: ``?status=pending,failed``) and the one ``filament_intake.fail_auto_source``
#: already uses for a source it cannot read: a ``cancelled`` router row is
#: invisible everywhere, which would hide the very thing this reason explains.
_AUTO_DEAD = "failed"


@dataclass(frozen=True, slots=True)
class DeleteImpact:
    """How many queued jobs deleting one original would touch (§10's pre-flight).

    ``pending_total`` is the two counts added up, and all three are **pending
    only**: a completed or cancelled row neither prints nor gets cancelled, so
    announcing it would describe rows that do not exist.
    """

    #: Jobs that keep printing — they hold a ``ready`` copy of their own bytes.
    self_contained: int = 0
    #: Jobs that would really be cancelled: legacy, preparing, broken or exempt.
    needs_original: int = 0

    @property
    def pending_total(self) -> int:
        return self.self_contained + self.needs_original


async def delete_impact(
    db: AsyncSession,
    *,
    archive_ids: list[int] | tuple[int, ...] = (),
    library_file_ids: list[int] | tuple[int, ...] = (),
) -> DeleteImpact:
    """The pre-flight: of the jobs naming these originals, who survives.

    Read-only, and deliberately **not** scoped to the caller's own rows: the
    question is what the delete will do, and it will do it to everybody's work.
    """
    self_contained = 0
    needs_original = 0
    for _row, ready in await _pending_rows(db, archive_ids=archive_ids, library_file_ids=library_file_ids):
        if ready:
            self_contained += 1
        else:
            needs_original += 1
    return DeleteImpact(self_contained=self_contained, needs_original=needs_original)


async def source_trashed(
    db: AsyncSession,
    *,
    archive_ids: list[int] | tuple[int, ...] = (),
    library_file_ids: list[int] | tuple[int, ...] = (),
    reason: str,
) -> int:
    """A soft delete: cancel only the pending jobs that cannot print without it.

    The navigational link is **kept**: a trashed row can be restored, and cutting
    the link here would destroy information the operator can still undo.

    Returns how many jobs were cancelled. Does not commit — the caller owns the
    transaction, as both delete paths always have.
    """
    return await _cancel_dependents(db, archive_ids=archive_ids, library_file_ids=library_file_ids, reason=reason)


async def source_purged(
    db: AsyncSession,
    *,
    archive_ids: list[int] | tuple[int, ...] = (),
    library_file_ids: list[int] | tuple[int, ...] = (),
    reason: str,
) -> int:
    """A hard delete: cancel the dependents, then cut the link on both tiers.

    ⚠️ **Order matters.** The cancel finds its rows *by* the id being deleted, so
    it runs before the detach that erases it.

    The detach is every status, not just pending — that is what the FK's
    ``SET NULL`` means on PostgreSQL, and doing it in code is the only way SQLite
    behaves the same (it enforces no FK action at all). A row whose id is left
    dangling is worse than untidy: it names a library file or archive that another
    row may later be given by rowid reuse.

    Returns how many jobs were cancelled. Does not commit.
    """
    cancelled = await _cancel_dependents(db, archive_ids=archive_ids, library_file_ids=library_file_ids, reason=reason)
    for model in (PrintQueueItem, AutoQueueItem):
        if archive_ids:
            await db.execute(update(model).where(model.archive_id.in_(archive_ids)).values(archive_id=None))
        if library_file_ids:
            await db.execute(
                update(model).where(model.library_file_id.in_(library_file_ids)).values(library_file_id=None)
            )
    return cancelled


async def independent_archive_bytes(db: AsyncSession, archive_id: int | None) -> str | None:
    """``None`` when this print's archive keeps its own copy of the bytes.

    Otherwise a sentence saying why it does not — §9 asks for "придатну незалежну
    архівну копію" before the automatic cleanup releases a job's reference, and the
    refusal has to be reportable, not just false.

    Three things are checked and a fourth deliberately is not:

    * the row is there and its ``file_path`` is filled — an empty one is the 3MF
      download's own retry marker, so those bytes are still on their way;
    * the file is on disk and not empty;
    * the path is **not inside the spool**. That is the load-bearing word: an
      archive pointing at the object itself is the same bytes, so releasing the
      blob against it would delete the file the archive is meant to keep.
    * The hash is **not** verified. §7 forbids hashing a large file on a routine
      path, and the collector does not verify a healthy object either; this is the
      same trade-off, made in the same direction.
    """
    if archive_id is None:
        return "this print has no archive row to keep a copy in"
    archive = await db.get(PrintArchive, archive_id)
    if archive is None:
        return "this print's archive row is gone"
    stored = (archive.file_path or "").strip()
    if not stored:
        return "its 3MF has not been attached to the archive yet"
    path = Path(stored)
    path = (
        path if path.is_absolute() else Path(settings.base_dir) / path
    )  # SEC-PATH-OK: relative_to(objects_root()) just below is the containment check
    try:
        path.relative_to(queue_sources.objects_root())
    except ValueError:
        pass
    else:
        return "the archive points at the queue's own copy rather than one of its own"
    size = await asyncio.to_thread(_size_or_none, path)
    if size is None:
        return "its 3MF is not on disk"
    if size == 0:
        return "its 3MF on disk is empty"
    return None


def _size_or_none(path: Path) -> int | None:
    """The file's size, or ``None`` when it is not a file we can read."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size if os.path.isfile(path) else None


async def _pending_rows(
    db: AsyncSession,
    *,
    archive_ids: list[int] | tuple[int, ...] = (),
    library_file_ids: list[int] | tuple[int, ...] = (),
) -> list[tuple[PrintQueueItem | AutoQueueItem, bool]]:
    """Every pending job naming one of these originals, with "is it ready?".

    ⚠️ **An ``assigned`` auto row is not here, and that is not an oversight.** It
    and the per-printer row it was promoted into are ONE piece of work: the
    printer-side row is the one that prints and the one this returns, so counting
    both would report two affected prints where there is one — and the number is
    the whole point of the sentence it goes into.
    """
    rows: list[tuple[PrintQueueItem | AutoQueueItem, bool]] = []
    if not archive_ids and not library_file_ids:
        return rows
    for model in (PrintQueueItem, AutoQueueItem):
        naming = []
        if archive_ids:
            naming.append(model.archive_id.in_(archive_ids))
        if library_file_ids:
            naming.append(model.library_file_id.in_(library_file_ids))
        found = await db.execute(
            select(model, QueueSource.state)
            .outerjoin(QueueSource, model.queue_source_id == QueueSource.id)
            .where(model.status == _PENDING, or_(*naming))
        )
        for row, blob_state in found.all():
            state = source_storage_state(
                queue_source_id=row.queue_source_id,
                blob_state=blob_state,
                origin=getattr(row, "origin", None),
                is_calibration=bool(getattr(row, "is_calibration", False)),
            )
            rows.append((row, state == "ready"))
    return rows


async def _cancel_dependents(
    db: AsyncSession,
    *,
    archive_ids: list[int] | tuple[int, ...],
    library_file_ids: list[int] | tuple[int, ...],
    reason: str,
) -> int:
    """Cancel the pending jobs that cannot print once this original is gone."""
    cancelled = 0
    for row, ready in await _pending_rows(db, archive_ids=archive_ids, library_file_ids=library_file_ids):
        if ready:
            # It keeps printing. Nothing to do — and nothing to say, either: the
            # card already shows that it holds its own copy.
            continue
        if isinstance(row, AutoQueueItem):
            row.status = _AUTO_DEAD
        else:
            row.status = "cancelled"
        row.waiting_reason = reason
        cancelled += 1
    if cancelled:
        logger.info("%d queued job(s) cancelled: %s", cancelled, reason)
    return cancelled
