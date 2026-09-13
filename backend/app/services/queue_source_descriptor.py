"""The ONE shape a queued job's source is read through — spec §4 and §7.

Spec: ``60-specs/queue-source-spool-spec.md``. §7 asks for a single internal
descriptor so that intake, metadata/plate validation, routing preview,
preflight, patching, upload and every repeat read *the same* bytes and agree on
what they are (S2). Today those paths each rebuild a path from
``archive.file_path`` or ``library_file.file_path`` and each decides for itself
what to do when the row is NULL; from Task 6 on they take a
:class:`QueueSourceDescriptor` instead.

**No I/O lives here.** The descriptor is a value: whoever builds it has already
done the reading, the hashing and the validation. Nothing in this module opens a
file, resolves a mount, or touches a session — a module that could would make
"just check it exists" reappear on the dispatch path, which §7 forbids after
capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from backend.app.core.config import settings
from backend.app.models.queue_source import STATE_READY

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.app.models.queue_source import QueueSource

#: What the API says about a job's source (spec §8). Add-only response field
#: ``source_storage``; the five values are closed.
#:
#: * ``ready`` — an attached blob that is verified and present. Set for THAT,
#:   never inferred from the kind of the original source.
#: * ``preparing`` — a live supervisor is capturing for this row right now.
#:   Derived, never stored: after a crash the same row reads ``legacy`` again.
#: * ``legacy`` — no snapshot yet; the row still depends on its original source
#:   and the background hydration will get to it (§8).
#: * ``broken`` — a snapshot was attached and its bytes no longer answer for it.
#: * ``exempt`` — nothing to snapshot: an external print BamDude never sent
#:   (including the adoption path), or a service calibration job (§2).
SourceStorageState = Literal["ready", "preparing", "legacy", "broken", "exempt"]
SOURCE_STORAGE_STATES: tuple[SourceStorageState, ...] = (
    "ready",
    "preparing",
    "legacy",
    "broken",
    "exempt",
)

#: The ``version`` stamped into ``PrintQueueItem.source_snapshot`` /
#: ``AutoQueueItem.source_snapshot`` (spec §4: the column is versioned).
#:
#: One named number, so that no writer can invent a second spelling of the
#: payload. ``services/filament_policy.py::VERSION`` is both the precedent and
#: the warning: its decoder compares the version for **exact** equality, so a
#: row written under an unrecognised version does not degrade gracefully — it
#: degrades silently. Bump this only together with a reader that accepts both.
SOURCE_SNAPSHOT_VERSION = 1

#: ``PrintQueueItem.origin`` values with no supported source at row creation.
#: ``direct`` is NOT one of them — BamDude sends a direct print from a local
#: sliced source and captures it like any other (§2, and the writers map's
#: ``capture`` classification of ``claim_printer_for_direct_print``).
_EXEMPT_ORIGINS = frozenset({"external"})


@dataclass(frozen=True, slots=True)
class QueueSourceDescriptor:
    """Everything a reader is allowed to know about a job's source.

    Frozen because S3 says the captured file is immutable once published: a
    reader that could re-point ``path`` would be a second source of truth about
    which bytes this job prints.

    ``path`` is the resolved absolute path of the captured object (the DB stores
    it relative to ``settings.base_dir``; resolving is the caller's job, exactly
    as ``filament_intake.resolve_source_path`` does it today).

    ``display_filename`` is the human name, carried separately on purpose: the
    hash may never replace it in the UI or on the printer, and two people may
    have queued the same bytes under different names (§4, A04).

    ``plate_fallback`` is the plate to use when the job itself names none — the
    archive's ``plate_index`` on an archive-sourced job, ``None`` otherwise.
    Plate authority stays on the job row; this is only the fallback.

    ``provenance`` is *immutable* metadata about where the bytes came from
    (``{"kind": "library_file", "id": 9}`` and friends). It is navigation and
    audit only: it is never read to decide whether two sources are the same —
    that is ``sha256``'s job alone (S4) — and it is not a second home for the
    job's own ``project_id`` / ``project_line_id`` / ``created_by_id``, which
    stay canonical on the row (§4).
    """

    path: Path
    format: str
    sha256: str
    size_bytes: int
    display_filename: str
    plate_fallback: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    #: The ``queue_sources`` row these bytes live in, when there is one — ``None``
    #: for a capture that has not been published yet (§5 step 4: the row is
    #: written by ``publish``, after the routing intent has already been
    #: serialized). It is recorded in the intent so a person can see which object
    #: the intent was written about, and it is **never** compared to decide
    #: whether two sources are the same: ``queue_sources.id`` is a plain INTEGER
    #: PRIMARY KEY, so SQLite hands a deleted row's id to the next INSERT, and
    #: only ``sha256`` answers that question (S4).
    queue_source_id: int | None = None


def source_snapshot(descriptor: QueueSourceDescriptor) -> dict[str, Any]:
    """The ``source_snapshot`` payload for a job backed by ``descriptor``.

    One builder, one layout — spec §4 names the contents (the original kind/id as
    provenance, the display filename, the format, the archive plate fallback), and
    a capture that hand-wrote the dict would be free to drop a key every later
    reader expects. Whoever attaches a queue source goes through here.

    Neither the hash nor the path is copied in: the bytes are identified by
    ``queue_source_id``, and a path duplicated into the row's JSON would be a
    second truth about where the file is — one that rots the moment the spool
    moves, which restore does. The ``provenance`` dict is copied, because the
    row's JSON is mutable and the descriptor is frozen.
    """
    return {
        "version": SOURCE_SNAPSHOT_VERSION,
        "provenance": dict(descriptor.provenance),
        "display_filename": descriptor.display_filename,
        "format": descriptor.format,
        "plate_fallback": descriptor.plate_fallback,
    }


def stored_descriptor(source: QueueSource, snapshot: dict[str, Any] | None) -> QueueSourceDescriptor:
    """Read back what :func:`source_snapshot` wrote — the job's own source.

    The mirror of the builder above, and deliberately the only one: a reader that
    picked the payload apart itself would be free to disagree about which key
    holds the plate.

    **The bytes come from the ROW, the names from the snapshot.** The row is the
    disk truth (the object may have been published long before this job existed,
    and ``restore`` moves the whole tree), while the filename and the plate
    fallback are the job's — two people may have queued the same bytes under
    different names (§4, A04).

    A payload written under another ``version`` is not parsed: the path, format,
    hash and size still come from the row, so the job still reads the **right
    bytes**, and only the display name and the plate fallback degrade to what the
    row alone can say. That is the opposite trade-off from
    ``filament_policy.deserialize_policy``, which must refuse an unknown version
    outright because there the payload IS the meaning; here it is provenance.
    """
    payload = snapshot if isinstance(snapshot, dict) and snapshot.get("version") == SOURCE_SNAPSHOT_VERSION else {}
    provenance = payload.get("provenance")
    return QueueSourceDescriptor(
        path=Path(settings.base_dir) / source.relative_path,
        format=source.format,
        sha256=source.sha256,
        size_bytes=source.size_bytes,
        # The object's own name is a hash and must never reach the UI or the
        # printer as one (§4) — but a row whose snapshot is missing or
        # unreadable has nothing better, and a reader that raised here would
        # take down a job whose bytes are perfectly fine.
        display_filename=payload.get("display_filename") or Path(source.relative_path).name,
        plate_fallback=payload.get("plate_fallback"),
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
        queue_source_id=source.id,
    )


def source_storage_state(
    *,
    queue_source_id: int | None,
    blob_state: str | None = None,
    origin: str | None = None,
    is_calibration: bool = False,
    preparing: bool = False,
) -> SourceStorageState:
    """What the API should say about one job row's source (spec §8).

    Pure: every input is a value the caller already has. ``blob_state`` is the
    attached :class:`~backend.app.models.queue_source.QueueSource`'s ``state``,
    or ``None`` when the caller did not load it — and a caller that does not
    know may not claim ``ready``, because ``ready`` is a promise that the bytes
    are there.

    ``origin`` / ``is_calibration`` are the ``PrintQueueItem`` columns; an
    ``AutoQueueItem`` has neither, and no auto row is exempt.

    ``preparing`` comes from the live capture supervisor. It is an argument and
    not a column on purpose: §5 rules out a persisted ``preparing`` status, so
    after a restart a row that was mid-capture reads ``legacy`` again and the
    background hydration picks it up.
    """
    if origin in _EXEMPT_ORIGINS or is_calibration:
        return "exempt"
    if preparing:
        return "preparing"
    if queue_source_id is None or blob_state is None:
        return "legacy"
    return "ready" if blob_state == STATE_READY else "broken"
