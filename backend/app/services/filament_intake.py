"""Shared source validation at the queue's write boundaries.

Since m173 a job's bytes are its own: a row with a ``queue_source_id`` reads the
captured copy under ``DATA_DIR/queue-sources`` and never its original again (spec
§7, S2). :func:`item_descriptor` is where that is decided, once, so that the
requirements reader, the routing writer, eligibility and preflight cannot each
answer it differently — and so that **no path falls back to the original after a
capture** (S7: a bad spool must not make anything read another file).
"""

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect, select

from backend.app.core.config import settings
from backend.app.i18n import current_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.queue_source import FORMAT_GCODE, QueueSource
from backend.app.services import swap_compatibility
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache
from backend.app.services.queue_source_descriptor import (
    SOURCE_SNAPSHOT_VERSION,
    QueueSourceDescriptor,
    stored_descriptor,
)
from backend.app.services.source_io import SourceUnavailable
from backend.app.utils.filament_types import SUPPORT_SLOT_TYPES, filament_types_compatible, support_slot_type


async def enrich_family_filament_rows(db, filaments: Iterable[dict]) -> list[dict]:
    """Attach each 3MF channel's family material without trusting its name."""
    from backend.app.services.filament_identity import resolve_raw, resolve_tray

    resolved_by_id = {}
    enriched = []
    for filament in filaments:
        family_id = filament.get("tray_info_idx")
        if family_id not in resolved_by_id:
            resolved = await resolve_tray(db, family_id)
            resolved_by_id[family_id] = resolved if resolved.family else await resolve_raw(db, family_id)
        resolved = resolved_by_id[family_id]
        filament_type = resolved.filament_type
        declared_type = (filament.get("filament_type") or filament.get("type") or "").strip().upper()
        if declared_type in SUPPORT_SLOT_TYPES and filament_types_compatible(
            declared_type.removesuffix("-S"), filament_type
        ):
            filament_type = declared_type
        filament_type = support_slot_type(filament_type, resolved.family.filament_id if resolved.family else None)
        if not filament_type:
            enriched.append(filament)
            continue
        enriched.append({**filament, "filament_type": filament_type})
    return enriched


async def enrich_family_filament_types(db, requirements: PrintRequirements) -> PrintRequirements:
    """Add family material to parsed requirements when the source was readable."""
    if requirements.status != "ok":
        return requirements
    filaments = await enrich_family_filament_rows(db, requirements.used_filaments)
    return replace(requirements, used_filaments=tuple(filaments))


async def fail_auto_source(db, view, reason) -> str:
    """Fail the router row ``view`` decided about -- only while it is still that row (G03 B, A17 step 3).

    One short transaction of its own, committed here (``auto_queue_eligibility.write_router_rows``): the row locked,
    re-read and compared with the view's incarnation and revision, then a compare-and-set on ``pending`` and not
    cancelled; a concurrent assignment, cancel, edit or source refresh stays authoritative. Returns ``written``,
    ``stale`` or ``gone``.
    """
    from backend.app.services.auto_queue_eligibility import write_router_rows

    values = {"status": "failed", "waiting_reason": routing_detail(reason)["message"]}
    return (await write_router_rows(db, {view.id: (view, values)}))[view.id]


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
    """The sentence an operator reads, plus the facts behind it when there are any.

    The facts ride as a SEPARATE sentence rather than as placeholders inside the
    44 existing ones: every caller that refuses without them keeps working, and
    a code whose message has no ``{}`` can be raised from anywhere as before.
    """
    lang = current_language()
    message = t(lang, "filament_routing", code, **params)
    if params.get("wanted"):
        if params.get("loaded"):
            detail = "detail.wanted_and_loaded"
        elif "loaded" in params:
            # Known to be empty — not the same as not asked. A grouped refusal
            # (one line for a whole printer model) passes no ``loaded`` at all,
            # because the trays it would name belong to one printer of many.
            detail = "detail.wanted_nothing_loaded"
        else:
            detail = "detail.wanted_only"
        message = f"{message} {t(lang, 'filament_routing', detail, **params)}"
    if params.get("print_tools"):
        # A direct rack's PrintTools the refusal is about (manual feed, a detached
        # tool) — a sentence of its own, so a caller without them reads no hole.
        message = f"{message} {t(lang, 'filament_routing', 'detail.print_tools', print_tools=params['print_tools'])}"
    return {"code": code, "params": params, "message": message}


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


async def item_descriptor(db, item, *, fresh: bool = False) -> QueueSourceDescriptor | None:
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

    ``fresh`` reads the blob and a pre-fix snapshot's own links again from the database rather than out of the
    session's identity map -- what a final claim compares with the answer it decided on.
    """
    source_id = getattr(item, "queue_source_id", None)
    if not source_id:
        return None
    source = await db.get(QueueSource, source_id, populate_existing=fresh)
    if source is None:
        return None
    descriptor = stored_descriptor(source, getattr(item, "source_snapshot", None))
    if descriptor.swap_fact.missing:
        # A FUTURE use of the job's bytes: its source's readers answer (an operator's declaration, §8's
        # classification). The completion of a run already executed asks ``execution_swap_fact`` instead.
        descriptor = replace(
            descriptor, swap_fact=await _legacy_swap_fact(db, item, descriptor, context="source", fresh=fresh)
        )
    return descriptor


@dataclass(frozen=True)
class SwapBasis:
    """What a decision on our swap macros was taken from: the job's incarnation, its blob and snapshot, and the
    EFFECTIVE fact with the hash it is bound to -- a pre-fix snapshot's fact may change with no change to the job."""

    identity: str | None
    queue_source_id: int | None
    snapshot: object
    fact: swap_compatibility.SourceFact | None
    sha256: str | None


def swap_basis(item, descriptor: QueueSourceDescriptor | None) -> SwapBasis:
    return SwapBasis(
        identity=swap_compatibility.job_identity(item),
        queue_source_id=getattr(item, "queue_source_id", None),
        snapshot=deepcopy(getattr(item, "source_snapshot", None)),
        fact=descriptor.swap_fact if descriptor is not None else None,
        sha256=descriptor.sha256 if descriptor is not None else None,
    )


async def swap_basis_holds(db, item, basis: SwapBasis) -> bool:
    """Whether the job, read again now -- the row, its blob and its source's fact, all fresh -- still has the basis
    a decision was taken from (swap-source-declaration spec r3 §6). Under the caller's claim; it reads, it writes
    nothing."""
    model = type(item)
    current = (
        await db.execute(select(model).where(model.id == item.id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if current is None:
        return False
    return swap_basis(current, await item_descriptor(db, current, fresh=True)) == basis


async def execution_swap_fact(db, item) -> swap_compatibility.SourceFact | None:
    """The declaration a job that already RAN was captured with -- for the completion's owed table swap.

    Its own snapshot answers; a snapshot from before the fix is resolved with historical evidence only, never a
    source's later declaration nor §8's classification (swap-source-declaration spec r3 §2). ``None`` for a job
    with no captured source.
    """
    source_id = getattr(item, "queue_source_id", None)
    if not source_id:
        return None
    source = await db.get(QueueSource, source_id)
    if source is None:
        return None
    descriptor = stored_descriptor(source, getattr(item, "source_snapshot", None))
    if not descriptor.swap_fact.missing:
        return descriptor.swap_fact
    return await _legacy_swap_fact(db, item, descriptor, context="execution")


async def _legacy_swap_fact(
    db, item, descriptor: QueueSourceDescriptor, *, context: str, fresh: bool = False
) -> swap_compatibility.SourceFact:
    """§6 of the swap-source-lifecycle spec: a snapshot written before the fix, answered from the job's own links.

    Only the job's live foreign keys are read -- a trashed row too, as metadata, since the link is the job's own;
    a link that is gone (``SET NULL``) is gone, and the provenance's id is never looked up (an id may have been
    reused). The answer is a value for this read: the stored snapshot is not rewritten. ``context`` -- required,
    no default -- says who asks: a future dispatch (``"source"``) or the completion of an executed run
    (``"execution"``).
    """
    kind = descriptor.provenance.get("kind")
    archive_id = getattr(item, "archive_id", None) if kind in (None, "archive") else None
    library_id = getattr(item, "library_file_id", None) if kind in (None, "library_file") else None
    return swap_compatibility.resolve_legacy(
        context=context,
        provenance_kind=kind,
        archive=await db.get(PrintArchive, archive_id, populate_existing=fresh) if archive_id else None,
        library_file=await db.get(LibraryFile, library_id, populate_existing=fresh) if library_id else None,
        sha256=descriptor.sha256,
    )


async def snapshot_for_copy(db, item, descriptor: QueueSourceDescriptor | None = None) -> dict | None:
    """The ``source_snapshot`` a job copied from ``item`` gets: a new dict, with the fact recorded.

    A clone, a copy and an assignment print the same bytes for the same reason, so they carry the same
    declaration -- resolved now, if the parent's predates the fix, and WRITTEN into the copy, so the copy never
    depends on the parent's links again (swap-source-lifecycle spec r4 §6). The parent's payload is not touched.
    A payload this version cannot read is copied as it is: it stays unreadable, and unknown, in the copy.
    ``descriptor`` is the read a caller already decided from: the copy then carries that very fact, never a second
    read's.
    """
    snapshot = getattr(item, "source_snapshot", None)
    if not isinstance(snapshot, dict) or snapshot.get("version") != SOURCE_SNAPSHOT_VERSION:
        return deepcopy(snapshot)
    if descriptor is None:
        descriptor = await item_descriptor(db, item)
    fact = descriptor.swap_fact if descriptor is not None else swap_compatibility.snapshot_fact(snapshot)
    return swap_compatibility.snapshot_with_fact(snapshot, fact)


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
        requirements = await (cache or PrintRequirementsCache()).read(
            descriptor.path,
            item.plate_id,
            archive_plate_id=descriptor.plate_fallback,
            # The identity this read reports is the snapshot's HASH, so the
            # revision it stamps into the intent — and the scheduler's two
            # claim-time re-probes — survive a restore (spec §7).
            sha256=descriptor.sha256,
        )
        return await enrich_family_filament_types(db, requirements)
    archive, library = await item_source(db, item)
    requirements = await (cache or PrintRequirementsCache()).read(
        resolve_source_path(archive, library), item.plate_id, archive_plate_id=archive.plate_index if archive else None
    )
    return await enrich_family_filament_types(db, requirements)


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
