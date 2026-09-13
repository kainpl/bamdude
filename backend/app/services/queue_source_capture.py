"""One add request, one captured source — the caller's half of spec §5.

Spec: ``60-specs/queue-source-spool-spec.md``. ``services/queue_sources.py`` owns
the copy and the publication; this module owns what the *queue writers* have to
do around them, so that the three producers (``queue_add``, ``auto_queue_add``,
``queue_batch``) share one answer to each of these:

* **what to capture** — :func:`plan_capture` turns a validated ``PrintArchive``
  or ``LibraryFile`` into a :class:`~backend.app.services.queue_sources.CaptureRequest`
  (the original's path, the verified container format, the human filename, the
  archive's plate fallback and the provenance the job row records);
* **how a failure is answered** — :func:`capture_staged` and
  :func:`publish_staged` map the service's taxonomy onto its own HTTP status
  (§6: 503 busy, 422 unreadable/changed/invalid, 504 timeout, 507 spool), with a
  localized message from the ``filament_routing`` namespace, where every other
  queue refusal the frontend reacts to already lives. **No route invents a
  status for these**, and no wrapper retries :func:`~backend.app.services.queue_sources.publish`:
  after a failure past the rename the bytes have been read once and are gone, so
  a failed add is reported once and the caller captures again.
* **where the requirements come from** — :func:`staged_requirements` reads them
  from the **staged copy** (§5 step 4). That is also where the requested plate is
  checked: the capture service deliberately does not know which plate a request
  needs, and this read (off the loop, by the child ``<metadata key="index">``
  rule inside ``filament_requirements``) refuses a plate the file does not have
  while the receipt is still in hand, so nothing is published.

The order every producer follows, and the reason for it:

1. permissions, identity, queue/printer/order choices — all before the copy;
2. **release the request's transaction**, then capture. A copy over a share can
   take minutes, and a request that held its read snapshot (on SQLite, the write
   lock) for that long is how one unreachable NAS stalls the app;
3. requirements/policy from the copy, refusals via :meth:`discard`;
4. rows written inside :func:`~backend.app.services.queue_sources.publish`'s own
   transaction, which commits the blob's row, the job rows and the counters
   together — after the file is in place;
5. re-read the rows in the caller's session, because the response is built from
   relationships only a live session can load.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.queue_source import FORMAT_3MF, FORMAT_GCODE, QueueSource
from backend.app.services import queue_sources
from backend.app.services.filament_intake import require_source_requirements, resolve_source_path, routing_detail
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache, SourceIdentity
from backend.app.services.queue_sources import CaptureReceipt, CaptureRequest, QueueSourceError
from backend.app.services.source_io import SourceUnavailable, source_probe

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StagedSource:
    """One request's captured bytes, with the little that is still known of the original.

    ``original`` and ``revision`` are about the *file the bytes came from*, not
    about the job: they exist for provenance and for the routing policy's
    revision (see :func:`staged_requirements`), and no dispatch path reads them.
    """

    receipt: CaptureReceipt
    original: Path
    revision: SourceIdentity | None = None

    @property
    def format(self) -> str:
        return self.receipt.format

    @property
    def staging_path(self) -> Path:
        return self.receipt.staging_path


def capture_format(path: Path) -> str:
    """The container format of the bytes on disk, from the SERVER's own path.

    The path, not ``filename``: the format decides the extension the object is
    stored under and the raw-gcode rules downstream (§4), so it has to describe
    the bytes rather than the name somebody typed. A source that is neither gets
    the same refusal the queue's own sliced-file gate gives, because that is what
    it is.
    """
    name = path.name.lower()
    if name.endswith(".gcode"):
        return FORMAT_GCODE
    if name.endswith(".3mf"):
        return FORMAT_3MF
    raise HTTPException(400, "Not a sliced file. Only .gcode or .gcode.3mf files can be printed.")


def plan_capture(*, archive=None, library_file=None, background: bool = False) -> CaptureRequest:
    """What to copy for a job sourced from this archive or library file.

    Pure: no filesystem call happens here (§5 step 2 keeps even a ``stat`` of an
    external path off the event loop — the worker does all of it). A source row
    with no file is refused with the same 422 the requirements reader has always
    given it.
    """
    source = archive or library_file
    path = resolve_source_path(archive, library_file)
    if source is None or path is None:
        raise HTTPException(422, routing_detail("source_unreadable", source_id=getattr(source, "id", None)))
    return CaptureRequest(
        path=path,
        format=capture_format(path),
        display_filename=source.filename,
        # Plate authority stays on the job row; this is only what a job that
        # names no plate falls back to, and only an archive has one (§4).
        plate_fallback=archive.plate_index if archive is not None else None,
        provenance={"kind": "archive" if archive is not None else "library_file", "id": source.id},
        background=background,
    )


async def capture_staged(request: CaptureRequest) -> StagedSource:
    """Copy the original into staging — §5 steps 2-3 — and answer §6's codes.

    The caller must already have released its DB transaction: this awaits a
    filesystem copy that may take minutes.
    """
    try:
        receipt = await queue_sources.capture(request)
    except QueueSourceError as exc:
        raise _refusal(exc) from exc
    return StagedSource(receipt=receipt, original=request.path, revision=await _original_revision(request.path))


async def publish_staged(
    staged: StagedSource,
    attach: Callable[[AsyncSession, QueueSource], Awaitable[None]],
) -> QueueSource:
    """Publish the staged bytes and write the caller's rows — §5 steps 5-7.

    Called **once**. A publication that failed after the rename has no bytes left
    to retry with (they were read once, and the object that is on disk may be
    shared with other owners), so a loop here would reach ``os.replace`` on
    nothing and escape the taxonomy as a bare ``OSError``. One failed add.
    """
    try:
        return await queue_sources.publish(staged.receipt, attach)
    except QueueSourceError as exc:
        raise _refusal(exc) from exc


async def discard_staged(staged: StagedSource | None) -> None:
    """Give up bytes that were captured for an add that is not going to happen.

    Safe to call on any outcome: a receipt that has already been published (or
    spent by a failure past the rename) owns no staged file and is left alone.
    """
    if staged is None or staged.receipt.state != "staged":
        return
    await staged.receipt.discard()


async def staged_requirements(
    staged: StagedSource,
    cache: PrintRequirementsCache,
    archive=None,
    library_file=None,
    plate_id: int | None = None,
    *,
    allow_raw_gcode: bool = False,
    product_plate_id: int | None = None,
) -> PrintRequirements | None:
    """Read the print requirements out of the CAPTURED bytes (§5 step 4).

    Everything the fan-out then writes — the resolved plate, the used channels,
    the model, the estimate — comes from the copy the job will actually print,
    and the refusal for a plate the file does not have happens here, between
    ``capture`` and ``publish``.

    The one thing that must **not** come from the copy is the file revision
    ``serialize_policy`` records. Until routing v2 anchors identity on the
    snapshot's hash, ``filament_preflight`` compares that revision against a
    fresh read of the ORIGINAL, and a staged copy's mtime there would defer every
    snapshot-backed job as ``source_changed`` — §7 says it outright: do not
    compare the original's size/mtime against the new copy's mtime. So the
    evidence is the copy's and the revision stays the original's.
    """
    req = await require_source_requirements(
        cache,
        archive,
        library_file,
        plate_id,
        allow_raw_gcode=allow_raw_gcode,
        product_plate_id=product_plate_id,
        source_path=staged.staging_path,
        source_format=staged.format,
    )
    return None if req is None else replace(req, source_identity=staged.revision)


async def _original_revision(path: Path) -> SourceIdentity | None:
    """The original's ``(size, mtime_ns)``, read once per request, off the loop.

    ``None`` when it can no longer be read — the original vanished in the moment
    between the copy and this read. The job is already independent of that file,
    and an absent revision is a comparison ``filament_preflight`` skips, exactly
    as it does for every row written before revisions existed. Refusing an add
    whose bytes are safely captured would be the wrong way round.
    """
    try:
        return await source_probe(("identity", str(path)), SourceIdentity.of, path)
    except SourceUnavailable as exc:
        logger.info("Captured %s, but its revision could no longer be read (%s)", path, exc.reason)
        return None


def _refusal(exc: QueueSourceError) -> HTTPException:
    """One mapping for the whole taxonomy — the status lives on the class (§6)."""
    return HTTPException(exc.http_status, routing_detail(exc.reason))
