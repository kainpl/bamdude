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
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor
from backend.app.services.queue_sources import CaptureReceipt, CaptureRequest, QueueSourceError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StagedSource:
    """One request's captured bytes, plus the path they came from.

    ``original`` is about the *file the bytes came from*, not about the job: it is
    provenance and a log line, and **no dispatch path reads it** — that is the
    whole point of having copied it. It deliberately carries no file revision any
    more: see :func:`staged_requirements` for why an mtime is the wrong identity
    for a frozen copy.
    """

    receipt: CaptureReceipt
    original: Path

    @property
    def format(self) -> str:
        return self.receipt.format

    @property
    def staging_path(self) -> Path:
        return self.receipt.staging_path

    @property
    def descriptor(self) -> QueueSourceDescriptor:
        """The staged bytes in the shape every reader takes (§7).

        Pointing at ``staging/<token>.part``, which is why the format travels
        beside it: the name carries no extension a reader could branch on. The
        published object gets its own descriptor from the row
        (``CaptureReceipt.descriptor``) — the two are never mixed, because only
        the row knows where the object finally landed.
        """
        return QueueSourceDescriptor(
            path=self.receipt.staging_path,
            format=self.receipt.format,
            sha256=self.receipt.sha256,
            size_bytes=self.receipt.size_bytes,
            display_filename=self.receipt.display_filename,
            plate_fallback=self.receipt.plate_fallback,
            provenance=dict(self.receipt.provenance),
        )


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
    return StagedSource(receipt=receipt, original=request.path)


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

    **No file revision is recorded on the intent**, and that is an answer rather
    than a gap. The ``(size, mtime_ns)`` pair ``serialize_policy`` stores exists
    to ask one question at dispatch — "has the original changed since this job was
    queued?" — and for a job that prints a frozen copy the question is obsolete:
    A03 says an existing job keeps the bytes it accepted, and a changed original
    is the *next* capture's business. Nor could it be asked honestly: §7 forbids
    comparing the original's size/mtime against the copy's, the two differ by
    construction, and after a ``restore`` an mtime is not a portable identity at
    all. ``filament_preflight`` skips the comparison when no revision was
    recorded, exactly as it does for every row written before revisions existed,
    and Task 7's routing v2 puts the snapshot's **hash** there instead — an
    identity that does survive a restore.

    Everything the same read produces for its own use is untouched: the cache
    still keys on the copy's identity, and ``auto_queue_scheduler``'s
    claim-time re-probe still compares the copy against itself, because the
    requirements it re-reads come from the same descriptor.
    """
    req = await require_source_requirements(
        cache,
        archive,
        library_file,
        plate_id,
        allow_raw_gcode=allow_raw_gcode,
        product_plate_id=product_plate_id,
        descriptor=staged.descriptor,
    )
    return None if req is None else replace(req, source_identity=None)


def _refusal(exc: QueueSourceError) -> HTTPException:
    """One mapping for the whole taxonomy — the status lives on the class (§6)."""
    return HTTPException(exc.http_status, routing_detail(exc.reason))
