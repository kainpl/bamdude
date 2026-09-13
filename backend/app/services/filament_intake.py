"""Shared source validation at the queue's write boundaries.

Since m173 a job's bytes are its own: a row with a ``queue_source_id`` reads the
captured copy under ``DATA_DIR/queue-spool`` and never its original again (spec
§7, S2). :func:`item_descriptor` is where that is decided, once, so that the
requirements reader, the routing writer, eligibility and preflight cannot each
answer it differently — and so that **no path falls back to the original after a
capture** (S7: a bad spool must not make anything read another file).
"""

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect, update

from backend.app.core.config import settings
from backend.app.i18n import current_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.queue_source import FORMAT_GCODE, QueueSource
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor, stored_descriptor
from backend.app.services.source_io import SourceUnavailable


async def fail_auto_source(db, item, reason):
    """Only fail an unclaimed item; a concurrent assignment/cancel stays authoritative."""
    await db.execute(
        update(AutoQueueItem)
        .where(AutoQueueItem.id == item.id, AutoQueueItem.status == "pending", AutoQueueItem.cancelled_at.is_(None))
        .values(status="failed", waiting_reason=routing_detail(reason)["message"])
    )


def resolve_source_path(
    archive=None, library_file=None, *, descriptor: QueueSourceDescriptor | None = None
) -> Path | None:
    """The bytes to read for this job: the captured copy when there is one.

    Keeps its name because it is still the one question ("which file?") asked by
    every reader — but a ``descriptor`` outranks both rows and short-circuits
    them: after a capture the original is not consulted, not even to check that
    it exists (§7).
    """
    if descriptor is not None:
        return descriptor.path
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


def loaded_descriptor(item) -> QueueSourceDescriptor | None:
    """:func:`item_descriptor` for a caller that cannot await — the response builders.

    Same question, same answer, same :func:`stored_descriptor`; the only
    difference is where the ``queue_sources`` row comes from. ``_enrich_response``
    and ``auto_queue._to_response`` are synchronous and run inside async request
    handlers, where touching an unloaded relationship is a ``MissingGreenlet``
    rather than a lazy query — so the row has to be eager-loaded by the query and
    this asks whether it was, exactly as the same builder already does for
    ``project``.

    A row whose relationship was not loaded reads as ``None``: the card then
    degrades to what the original rows can say, which is what it did before m173.
    That is a display decision only; nothing here is on a dispatch path.
    """
    if not getattr(item, "queue_source_id", None):
        return None
    if "queue_source" in sa_inspect(item).unloaded:
        return None
    source = item.queue_source
    return None if source is None else stored_descriptor(source, getattr(item, "source_snapshot", None))


async def item_descriptor(db, item) -> QueueSourceDescriptor | None:
    """The captured source of an existing job row, or ``None`` for a legacy one.

    ``None`` means exactly one thing: this row has no ``queue_source_id``, so it
    is a pre-m173 job (or one of §2's exemptions) and its original is still the
    only source it has. It does **not** mean "the snapshot did not work out" —
    a row that names a blob is answered from that blob whatever state the blob is
    in, because §7/S7 forbid quietly reading a different file instead: a broken or
    missing object surfaces as this job's own ``source_unreadable``, naming it,
    rather than as a print of something else.

    The one exception is a ``queue_source_id`` whose row is *gone*, which the
    RESTRICT foreign key makes impossible on PostgreSQL and which the GC never
    does on either dialect (it refuses to release an owned blob). If it ever
    happens the job reads as legacy — its own original, never a stranger's.
    """
    source_id = getattr(item, "queue_source_id", None)
    if not source_id:
        return None
    source = await db.get(QueueSource, source_id)
    if source is None:
        return None
    return stored_descriptor(source, getattr(item, "source_snapshot", None))


def source_display_filename(descriptor: QueueSourceDescriptor) -> str:
    """The human name a captured source may be printed under — or a refusal.

    ``stored_descriptor`` has to answer *something* when it cannot parse a row's
    ``source_snapshot`` (a payload written under a future
    ``SOURCE_SNAPSHOT_VERSION``, i.e. after a downgrade), and what it answers is
    the object's own file name — which is its sha256. Its docstring scopes that
    fallback to "only the display name and the plate fallback degrade", and for a
    label on a screen that is true.

    It is **not** true for the dispatch, which is why this asks before using it.
    That one string decides whether the file looks sliced at all
    (``_is_sliced_file``) and becomes the name on the printer via
    ``derive_remote_filename`` — and §4/A04 forbid the hash reaching the printer
    or the UI as a filename. So a job whose payload this version cannot read fails
    closed with ``source_unreadable``: the same refusal a job whose object is
    missing gets, and for the same reason — this BamDude cannot read that job's
    source. It is recoverable by upgrading back, and it never sends a fabricated
    name to hardware.

    Recognising it needs no access to the payload: the fallback is *exactly* the
    object's basename, and no capture ever records a display name equal to the
    hash it computed.
    """
    if descriptor.display_filename == descriptor.path.name:
        raise SourceUnavailable()
    return descriptor.display_filename


async def read_item_requirements(db, item, cache: PrintRequirementsCache | None = None) -> PrintRequirements:
    descriptor = await item_descriptor(db, item)
    if descriptor is not None:
        # No ``item_source`` call at all: the archive and library rows are
        # navigation now, and one of them may be trashed or gone — which for a
        # snapshot-backed job changes nothing about what it prints.
        return await (cache or PrintRequirementsCache()).read(
            descriptor.path,
            item.plate_id,
            archive_plate_id=descriptor.plate_fallback,
            # The identity this read reports is the snapshot's HASH, so the
            # revision it stamps into the intent — and the scheduler's two
            # claim-time re-probes — survive a restore (spec §7).
            sha256=descriptor.sha256,
        )
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
    descriptor: QueueSourceDescriptor | None = None,
) -> PrintRequirements | None:
    """Requirements for a queue writer's source, or a 422 naming what is wrong.

    ``descriptor`` is the *bytes to read instead of the original*: a capture's
    staged copy while an add is being decided (spec §5 step 4) or the published
    object of a job that already has one (§7). It carries the format because a
    staged file is called ``<token>.part`` and cannot be asked what container it
    is, and the plate fallback because the archive row that used to supply it may
    be gone by the time an existing job is re-read.

    The ``archive`` / ``library_file`` rows stay in the signature even with a
    descriptor: the refusal names that id, which is what the operator recognises.
    """
    source = archive or library_file
    path = resolve_source_path(archive, library_file, descriptor=descriptor)
    raw_gcode = (
        descriptor.format == FORMAT_GCODE
        if descriptor is not None
        else path is not None and path.suffix.lower() == ".gcode"
    )
    # Only a server-identified raw G-code source keeps the existing explicit
    # per-printer workflow. A broken 3MF never acquires this exemption.
    if allow_raw_gcode and raw_gcode:
        return None
    archive_plate_id = (
        descriptor.plate_fallback if descriptor is not None else (archive.plate_index if archive else None)
    )
    req = await cache.read(
        path, plate_id, archive_plate_id=archive_plate_id, sha256=descriptor.sha256 if descriptor else None
    )
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
