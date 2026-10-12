import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
from pathlib import Path
from uuid import uuid4

from defusedxml import ElementTree as ET
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.core.tasks import spawn_background_task
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.archive_parts import seed_archive_parts
from backend.app.services.library_helpers import skip_objects_supported_from_metadata
from backend.app.services.threemf_parser_core import (  # noqa: F401 - stable archive re-exports
    ThreeMFParser,
    _coerce_bool,
    _name_for,
    _objects_from_gcode_header,
    _objects_from_pick_png,
    _objects_from_slice_info,
    _pick_centroids_from_3mf,
    build_plate_objects_payload,
    discover_plate_objects,
    extract_printable_objects_from_3mf,
    extract_skip_support_from_3mf,
    parse_per_plate_skip_metadata,
    parse_plates_from_3mf,
    read_total_layers,
)
from backend.app.utils.ffmpeg_output import NO_FFMPEG_OUTPUT, summarize_ffmpeg_stderr
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

logger = logging.getLogger(__name__)


@dataclass
class _PreparedArchiveAttach:
    """Filesystem facts prepared before the archive writer is entered.

    The staging directory lives under the target printer's archive root, so a
    successful publication can atomically rename the copied 3MF into its final
    directory. No ORM object crosses this boundary: the row is reloaded under
    the short writer guard immediately before publication.
    """

    stage_dir: Path
    staged_file: Path
    content_hash: str
    dest_name: str
    display_stem: str
    requested_plate_index: int | None
    parser_plate_index: int | None
    metadata: dict
    plates_payload: list | None
    printable_objects: dict | None


class _AttachPreparationStale(RuntimeError):
    """The row's selected plate changed after the off-lock preparation."""


def _prepare_archive_attach_file(
    source_file: Path,
    original_filename: str | None,
    archive_root: Path,
    printer_folder: str,
    plate_index: int | None,
) -> _PreparedArchiveAttach:
    """Copy and inspect a recovered 3MF without holding a DB writer.

    This runs in a worker thread. The source may be large and ZIP parsing is
    CPU/blocking I/O; keeping both outside ``archive_write_scope`` prevents an
    attach retry from stalling unrelated API/MQTT work or SQLite's sole writer.
    """
    dest_name = original_filename or source_file.name
    display_stem = resolve_display_stem(dest_name)
    stage_dir = archive_root / printer_folder / ".attach-staging" / uuid4().hex
    staged_file: Path | None = None
    try:
        stage_dir.mkdir(parents=True)
        staged_file = safe_join_under(stage_dir, dest_name, http=False)
        _copy_and_fsync(source_file, staged_file)
        if (
            source_file.suffix.lower() == ".3mf"
            and zipfile.is_zipfile(source_file)
            and not zipfile.is_zipfile(staged_file)
        ):
            raise ValueError("copied 3MF is not a valid ZIP")

        parser = ThreeMFParser(staged_file, plate_number=plate_index)
        metadata = parser.parse()
        try:
            with zipfile.ZipFile(staged_file, "r") as zfh:
                plates_payload = parse_plates_from_3mf(zfh)
        except Exception as exc:  # noqa: BLE001 - per-plate data is optional
            logger.debug("attach preparation: per-plate parse failed: %s", exc)
            plates_payload = None

        try:
            printable_objects = extract_printable_objects_from_3mf(staged_file.read_bytes(), plate_number=plate_index)
        except Exception as exc:  # noqa: BLE001 - part ledger is best effort
            logger.warning("attach preparation: printable-object parse failed for %s: %s", staged_file, exc)
            printable_objects = None

        return _PreparedArchiveAttach(
            stage_dir=stage_dir,
            staged_file=staged_file,
            # Hash the fsync'd staging copy, not a mutable FTP-temp source:
            # publication's DB identity must describe the exact bytes about to
            # be renamed into the archive.
            content_hash=ArchiveService.compute_file_hash(staged_file),
            dest_name=dest_name,
            display_stem=display_stem,
            requested_plate_index=plate_index,
            parser_plate_index=parser.plate_number,
            metadata=metadata,
            plates_payload=plates_payload,
            printable_objects=printable_objects if isinstance(printable_objects, dict) else None,
        )
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _discard_prepared_archive_attach(prepared: _PreparedArchiveAttach) -> None:
    """Remove only this invocation's staging directory."""
    shutil.rmtree(prepared.stage_dir, ignore_errors=True)


async def _attach_output_is_durably_referenced(
    db: AsyncSession,
    archive_id: int,
    file_path: str | None,
) -> bool | None:
    """Check a possibly ambiguous attach commit through a fresh connection.

    ``None`` means the outcome could not be checked.  Cleanup must preserve
    the output in that case: deleting a file after a commit that reached the
    database but whose acknowledgement was lost is worse than one diagnosable
    orphan directory.
    """

    if not file_path or db.bind is None:
        return None
    try:
        verifier = async_sessionmaker(db.bind, expire_on_commit=False)
        async with verifier() as verify_db:
            return (
                await verify_db.scalar(
                    select(PrintArchive.id).where(
                        PrintArchive.id == archive_id,
                        PrintArchive.file_path == file_path,
                        PrintArchive.deleted_at.is_(None),
                    )
                )
            ) is not None
    except Exception:  # noqa: BLE001 - uncertainty intentionally preserves bytes
        logger.exception("attach_3mf_to_archive could not verify ambiguous commit for archive %s", archive_id)
        return None


def _copy_and_fsync(src: Path, dst: Path, chunk_size: int = 1024 * 1024) -> None:
    """Copy ``src`` to ``dst`` with an explicit chunked read/write and fsync the dst.

    Replacement for ``shutil.copy2`` in the archive pipeline. ``shutil.copy2``
    uses Linux ``sendfile()``, which on some kernels/filesystems has returned
    a short count on the first call and truncated the destination for larger
    3MF uploads (#1032, observed on Raspberry Pi OS bookworm / armv7l). An
    explicit loop with ``fsync`` avoids that path and guarantees the dest
    bytes are on disk before the caller inspects them as a ZIP.
    """
    with src.open("rb") as rf, dst.open("wb") as wf:
        while True:
            buf = rf.read(chunk_size)
            if not buf:
                break
            wf.write(buf)
        wf.flush()
        os.fsync(wf.fileno())
    shutil.copystat(src, dst)


def resolve_display_stem(filename: str) -> str:
    """Return a clean human-readable stem from a 3MF/gcode filename (#1152).

    Bambu Studio's "Send to printer" dialog typically writes files like
    ``Plate_1.gcode.3mf`` (a sliced gcode payload wrapped in a 3MF container).
    The naive ``Path(filename).stem`` only drops the last suffix, leaving
    ``Plate_1.gcode`` — which then surfaces in the archive UI / timelapse
    name-match path as a confusing ``Plate_1.gcode`` rather than ``Plate_1``.

    Strip the recognised print-format suffixes in order (case-insensitive):

    - ``.gcode.3mf`` → bare stem (Bambu Studio FTP send)
    - ``.3mf``       → bare stem
    - ``.gcode``     → bare stem (rare standalone gcode upload)

    Anything else passes through ``Path(...).stem`` unchanged. Path components
    are stripped first so callers can pass either a basename or a full path.
    """
    name = Path(filename).name
    lower = name.lower()
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def archive_storage_stem(display_stem: str) -> str:
    """Return a filesystem-safe directory component without changing its label.

    Windows silently removes trailing spaces and dots from a directory name.
    If we create such a directory from the display stem, a later ``resolve``
    can describe parent and child with incompatible path representations and
    make ``safe_join_under`` reject an otherwise legal filename.  The archive
    keeps the original filename and display name; this is only its directory
    component.
    """
    return display_stem.rstrip(" .") or "print"


def create_archive_directory(parent: Path, display_stem: str, *, timestamp: str | None = None) -> Path:
    """Create one unique archive directory and return its canonical path.

    ``mkdir`` is the collision gate.  An ``exists`` check alone is racy when
    two recovery paths land in the same second.
    """
    base_name = f"{timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')}_{archive_storage_stem(display_stem)}"
    for suffix in count(1):
        name = base_name if suffix == 1 else f"{base_name}_{suffix}"
        candidate = parent / name  # SEC-PATH-OK: component is timestamp + path-stripped normalized display stem
        try:
            candidate.mkdir(parents=True)
            return candidate.resolve()
        except FileExistsError:
            continue


def sliced_plate_indices_in_3mf(file_path: Path) -> set[int]:
    """Every plate index a sliced 3MF's ``slice_info.config`` records, or an empty set.

    The retry service's plate check (upstream a4cfbd42 part 3): a candidate
    whose plates do not include the archive's own is another plate's file. A
    set rather than ``peek_plate_index_in_3mf``'s first plate, because a
    "slice all" upload records every plate and must not read as plate 1. Empty
    on anything unreadable — no answer is not a contradiction.
    """
    plates: set[int] = set()
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return plates
            root = ET.fromstring(zf.read("Metadata/slice_info.config").decode())
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ET.ParseError):
        return plates
    for plate in root.findall(".//plate"):
        for meta in plate.findall("metadata"):
            if meta.get("key") == "index":
                try:
                    plates.add(int(meta.get("value") or ""))
                except ValueError:
                    pass
    return plates


def peek_plate_index_in_3mf(file_path: Path) -> int | None:
    """Return the plate index recorded inside a Bambu 3MF, or None (#1204).

    Reads only ``Metadata/slice_info.config`` to keep this cheap — used by
    the print-start callback to verify that the 3MF we just downloaded over
    FTP actually matches the plate the printer is running. The full
    ``ThreeMFParser`` does much more work and runs later inside
    ``ArchiveService``; this is a one-shot peek for the validation gate.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None
            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)
            plate = root.find(".//plate")
            if plate is None:
                return None
            for meta in plate.findall("metadata"):
                if meta.get("key") == "index":
                    value = meta.get("value")
                    if value:
                        try:
                            return int(value)
                        except ValueError:
                            return None
    except Exception:
        return None
    return None


_PLATE_SUFFIX_RE = re.compile(r"^(.*?)(\s*-\s*Plate\s+|_plate_)(\d+)$", re.IGNORECASE)


def swap_plate_suffix(name: str | None, target_plate: int) -> str | None:
    """Return ``name`` with its trailing plate number replaced, or None (#1204).

    Bambu Studio names multi-plate uploads ``"<Project> - Plate <N>"`` (and
    a lowercase ``"_plate_<N>"`` variant exists too). When MQTT
    ``subtask_name`` lags across consecutive plates of the same model the
    suffix points at the previous plate; swapping it gives us the correct
    upload to re-fetch from FTP. Returns ``None`` if no recognised suffix
    is present so the caller can fall through to the no-3MF archive path.
    """
    if not name:
        return None
    m = _PLATE_SUFFIX_RE.match(name)
    if not m:
        return None
    base, separator, _ = m.groups()
    return f"{base}{separator}{target_plate}"


def sd_stem(sd_name: str) -> str:
    """The name on the card without its trailing ``.gcode.3mf`` / ``.3mf`` suffixes.

    The same strip loop as ``utils.filename.derive_remote_filename``, minus the
    re-append of a single ``.3mf`` — the two must agree on where the stem ends,
    because that function computes the very name on the card this one takes apart.

    Raises ``TypeError`` on non-string input rather than entering the loop, for
    the reason that function records: a duck-typed object whose ``endswith``
    returns a truthy sentinel never escapes, and the unbounded allocation that
    follows has cgroup-OOM'd the test runner under mocks.
    """
    if not isinstance(sd_name, str):
        raise TypeError(f"sd_stem requires str, got {type(sd_name).__name__}")
    stem = sd_name
    while True:
        if stem.endswith(".gcode.3mf"):
            stem = stem[:-10]
        elif stem.endswith(".3mf"):
            stem = stem[:-4]
        else:
            return stem


def _reconstruct_recovered_start(archive: PrintArchive) -> bool:
    """Set ``started_at`` for a print adopted mid-flight (spec 2026-09-12 §3.3).

    The row was created when BamDude joined the print, with the remaining time
    the printer reported at that moment; the slicer's estimate arrives with the
    3MF. ``started_at = observed_at − (estimate − remaining)``; when the printer
    claims more remaining than the whole estimate (firmware drift) the honest
    floor is ``observed_at``. Missing inputs leave the start unknown — nothing
    downstream banks a fictitious duration for a row without ``started_at``.
    Returns True when it wrote the field.

    ⚠️ Every refusal is a no-op, never an exception. The only caller is
    ``attach_3mf_to_archive``, whose broad ``except`` rolls the session back and
    returns False — so a record this could not read would cost the whole attach
    and orphan the 3MF folder already copied to disk, to save a timestamp.
    A ``remaining_seconds`` that is a bool is treated as missing: ``True`` is an
    ``int`` in Python, and reading a flag as "one second left" would invent a
    whole print's worth of elapsed time.
    """
    extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
    rec = extra.get("recovered_start")
    if not isinstance(rec, dict) or archive.started_at is not None:
        return False
    remaining = rec.get("remaining_seconds")
    estimate = archive.print_time_seconds
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
        return False  # ``json.loads`` admits NaN/Infinity literals; ``int()`` of either would raise below
    if not estimate or estimate <= 0:
        return False
    try:
        observed_at = datetime.fromisoformat(rec.get("observed_at"))
    except (TypeError, ValueError):
        logger.warning(
            "recovered_start for archive %s has an unreadable observed_at (%r) — leaving started_at unknown",
            archive.id,
            rec.get("observed_at"),
        )
        return False
    # The records are written in UTC; a value that lost its offset on the way
    # through the database is still that instant, not a local-time one.
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    elapsed = max(0, int(estimate) - int(remaining))
    archive.started_at = observed_at - timedelta(seconds=elapsed)
    archive.extra_data = {**extra, "started_at_reconstructed": True}
    return True


async def find_archive_for_sd_file(db: AsyncSession, printer_id: int, sd_name: str) -> PrintArchive | None:
    """The archive that still has something to show for a file on the card — or None.

    One owner for "printer file name → archive" (spec 2026-09-12 §3.1): the
    file manager asks here, and the printer-card cover asks here only as its
    fallback — first it takes the running print's own archive by id
    (``routes/printers.resolve_current_archive_id``), because a name is how to
    find an archive when nobody knows which one it is. An archive's
    ``filename`` IS the card name for an external print (the download attaches
    the name it found) and DERIVES the card name for a dispatched one
    (``derive_remote_filename``: suffixes collapsed, spaces → underscores), so
    the match folds spaces on both sides and accepts ``{stem}.3mf`` /
    ``{stem}.gcode.3mf`` / the raw name / ``print_name == stem``. No content
    hash: that would need the bytes, and not fetching them is the point.

    A candidate counts when it still has **something on disk to show** — its
    3MF, or failing that its extracted ``thumbnail_path``. Two kinds of row have
    no 3MF and they are not the same thing:

    * *pending* — the row is created at print start with ``file_path=""`` and no
      thumbnail yet, while the 3MF is still being fetched. It has nothing to
      show and must never shadow an older populated row; having neither a path
      nor a thumbnail, it is already excluded in SQL.
    * *cleaned* — retention (``archive_cleanup_service``) deleted the 3MF and
      blanked ``file_path`` but deliberately KEPT ``thumbnail_path``, because
      the row is print history. Amended 2026-09-12: that surviving picture is a
      valid answer and gets used.

    A **trashed** row (``deleted_at`` set) never answers either, newest or not:
    it falls through to the next candidate exactly as a vanished file does.
    ``PrintArchive.active()`` is the rule — trash must not leak into a normal
    flow — and the sibling URL the plates route hands out
    (``/archives/{id}/plate-thumbnail/{n}``) refuses a trashed row anyway.

    Newest ``created_at`` first, and a row whose files have since vanished
    yields to the next — so the walk reads light ``(id, file_path,
    thumbnail_path)`` tuples and loads only the winner as an entity. No blind
    ``LIMIT``: the eleventh row can be the only one still on disk.

    Deliberately no status filter: the printer can be FINISH (the archive
    already flipped to ``completed``) while the UI still asks for the picture.

    **Callers decide what they can serve from the answer.** The cover serves
    ``thumbnail_path`` when the row has one and opens the 3MF otherwise; the
    plates route serves the cached ``extra_data["plates"]`` plus the printed
    plate's PNG, and falls through to one read from the printer when a cleaned
    row has no cached plates either.

    ⚠️ Known hole, recorded rather than fixed: a #1542 doubled-suffix filename
    (``Model.gcode.3mf.gcode.3mf`` stored on the row) matches no arm — the stem
    of the card name is ``Model``, and folding spaces does not collapse the
    extra suffix. The download stopped producing such names (2026-09-30,
    ``archive_download.build_filename_candidates``); rows stored before keep
    them, and a library file made from such a row carries them into every
    archive dispatched from it. The consequence is graceful: the plates route
    reads the file once, as it does for a file that never printed, and the
    cover of a running print no longer depends on this match.
    """
    stem = sd_stem(sd_name)
    stem_us = stem.replace(" ", "_")
    filename_us = func.replace(PrintArchive.filename, " ", "_")
    candidates = (
        await db.execute(
            select(PrintArchive.id, PrintArchive.file_path, PrintArchive.thumbnail_path)
            .where(PrintArchive.printer_id == printer_id)
            .where(PrintArchive.deleted_at.is_(None))
            .where(or_(PrintArchive.file_path != "", PrintArchive.thumbnail_path.is_not(None)))
            .where(
                or_(
                    PrintArchive.print_name == stem,
                    PrintArchive.filename == sd_name,
                    filename_us == f"{stem_us}.3mf",
                    filename_us == f"{stem_us}.gcode.3mf",
                )
            )
            .order_by(PrintArchive.created_at.desc())
        )
    ).all()
    for archive_id, file_path, thumbnail_path in candidates:
        if archive_has_something_on_disk(file_path, thumbnail_path):
            return await db.get(PrintArchive, archive_id)
    return None


def archive_has_something_on_disk(file_path: str | None, thumbnail_path: str | None) -> bool:
    """True when an archive's 3MF or its extracted PNG is on disk — something to show.

    ``base_dir / ""`` is base_dir itself — a directory, so is_file() is False —
    but check the column first and say so out loud. Both columns are persisted
    server-owned relative paths, written only by archive_print /
    attach_3mf_to_archive / the thumbnail extractor — never by a request.
    """
    local_3mf = settings.base_dir / file_path if file_path else None  # SEC-PATH-OK: server-owned column
    local_png = settings.base_dir / thumbnail_path if thumbnail_path else None  # SEC-PATH-OK: server-owned column
    return (local_3mf is not None and local_3mf.is_file()) or (local_png is not None and local_png.is_file())


def remove_swap_pending_event(archive: PrintArchive, event: str) -> bool:
    """Drop *event* from ``archive.extra_data['swap_macro_events_pending']``.

    Used by dispatch right after firing ``swap_mode_start`` and by
    ``on_print_complete`` right after firing ``swap_mode_change_table``
    so the pending list is a real checklist of what's left to do — not a
    static record of what was originally planned. The caller is responsible
    for committing the session.

    If removing *event* leaves the list empty, the key is dropped
    entirely so a future on_print_complete sees a clean ``extra_data``.

    Returns True iff *event* was present (i.e. caller has dirty state to
    commit). Returns False if the event was already absent (idempotent
    no-op — second call can't double-fire).
    """
    if not isinstance(archive.extra_data, dict):
        return False
    pending = archive.extra_data.get("swap_macro_events_pending")
    if not isinstance(pending, list) or event not in pending:
        return False
    remaining = [e for e in pending if e != event]
    merged = dict(archive.extra_data)
    if remaining:
        merged["swap_macro_events_pending"] = remaining
    else:
        merged.pop("swap_macro_events_pending", None)
    archive.extra_data = merged
    return True


def set_selected_macro_ids(archive: PrintArchive, macro_ids: list[int] | None) -> None:
    """Record the per-print macro selection on the archive.

    The copy that survives a backend restart between print start and print
    finish; the in-memory registration in ``main`` covers the earlier window,
    before this row exists at all.

    ``None`` writes nothing. A dispatch that carried no selection must not be
    made to look like one that chose an empty set — both fire nothing, but
    only one of them was a decision, and the row is where that is legible.
    """
    if macro_ids is None:
        return
    merged = dict(archive.extra_data or {})
    merged["selected_macro_ids"] = [int(i) for i in macro_ids]
    archive.extra_data = merged


def add_fired_layer_macro(archive: PrintArchive, macro_id: int) -> bool:
    """Record *macro_id* in ``archive.extra_data['layer_macros_fired']``.

    The mirror image of :func:`remove_swap_pending_event`. That one keeps a
    checklist of what is still owed, because swap intent is fixed at dispatch.
    This one keeps a record of what already ran, because the set of layer
    macros is *not* fixed at dispatch — a macro can be added or edited
    mid-print — so "what was planned" would be a lie by the time it mattered.

    The record survives a backend restart, which the in-memory guard cannot.
    It has to: after a restart the MQTT state starts at layer 0 and the next
    report looks like a jump from 0, re-crossing every target in the print.

    Returns True iff the id was newly added (i.e. the caller has dirty state
    to commit, and the macro has not run yet). Returns False if it was already
    there — an idempotent no-op, so a second call can't double-fire.
    """
    existing = archive.extra_data if isinstance(archive.extra_data, dict) else {}
    raw = existing.get("layer_macros_fired")
    fired = [int(m) for m in raw] if isinstance(raw, list) else []
    if macro_id in fired:
        return False
    merged = dict(existing)
    merged["layer_macros_fired"] = [*fired, macro_id]
    archive.extra_data = merged
    return True


def load_objects_from_archive_into_state(archive: PrintArchive, printer_id: int, *, is_retrigger: bool = False) -> bool:
    """Parse the archive's stored 3MF and push printable_objects into MQTT state.

    Used by ``main.on_print_start`` and the on-demand
    ``ArchiveDownloadRetryService`` so the skip-objects modal sees objects
    even on prints that started from the printer (where BamDude's initial
    FTP fetch may have failed and only succeeded on a later retry).

    State is reset unconditionally as soon as the function is called for a
    given archive — we're declaring "this is the current print on
    *printer_id*". Stale ``printable_objects`` from the prior print would
    otherwise misreport the count and let the frontend show the skip
    dialog with object IDs that don't exist in the new gcode (the firmware
    would reject the resulting ``M623`` with "Invalid object IDs"). If the
    parser yields nothing (Orca with ``support_skip_objects`` disabled, a
    corrupt 3MF, or a non-3MF archive), the state stays empty — the
    skip-objects button stays hidden, which is the truthful UI for "we
    don't know the object list for this print".

    ``is_retrigger=True`` says "this is the print already in state, told to me
    again" — a reconnect re-firing ``on_print_start``. Then an unreadable file
    means do nothing at all, rather than reset: there is no previous print's
    data to protect against, and the reset would throw away the current
    print's. Leave it False whenever the archive might be a *different* print
    from whatever the state holds; the reset is what stops one print's ids
    being offered for another's plate.

    Returns True iff non-empty objects were loaded.
    """
    # Local import — printer_manager pulls in archive_download_retry which
    # in turn would close a load-time cycle if we imported at module top.
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)

    file_path = settings.base_dir / archive.file_path if archive.file_path else None
    readable = file_path is not None and file_path.is_file() and str(file_path).endswith(".3mf")

    if not readable and is_retrigger:
        # ⚠️ Leave the state alone. The caller is re-declaring the print it is
        # ALREADY holding (a reconnect re-fires ``on_print_start``), so the
        # objects in state belong to this same print — wiping them here can
        # only lose good data, never protect anything.
        #
        # This became reachable when the archive row moved to PRINT START: the
        # row now exists with ``file_path=""`` for the whole download window,
        # which on a slow printer is minutes (22 MB off a P1S measured 8m40s
        # mid-print). Any reconnect in that window used to land here, blank the
        # objects, and leave the Skip button dark for the rest of the plate —
        # seen on 3 of 4 machines printing the same file.
        return False

    if client is not None:
        client.state.printable_objects = {}
        client.state.printable_objects_bbox_all = None
        client.state.printable_objects_approximate = False
        client.state.skipped_objects = []
        client.state.skip_objects_supported = False

    try:
        if not readable:
            return False
        with open(file_path, "rb") as f:
            threemf_data = f.read()
        # The archive knows which plate it printed, so scope to it (#2522) — a
        # multi-plate 3MF numbers identify_ids per plate, and an unscoped list
        # would offer plate 1's objects for a plate-3 job.
        printable_objects, bbox_all, approximate = extract_printable_objects_from_3mf(
            threemf_data,
            plate_number=archive.plate_index,
            include_positions=True,
            with_confidence=True,
        )
        if not printable_objects:
            return False
        if client is None:
            return False
        client.state.printable_objects = printable_objects
        client.state.printable_objects_bbox_all = bbox_all
        client.state.printable_objects_approximate = approximate

        # skip_objects_supported gate: requires BOTH flags true in
        # archive.extra_data. Strict — None / missing → False (hide
        # the button). For Bambu Studio files the parser defaults
        # gcode_label_objects to True at extraction time; exclude_object
        # is only stored when interpretable. Old archives without the
        # m022 backfill will fail this gate cleanly (button hidden, no
        # firmware-reject surprises on click).
        meta = archive.extra_data if isinstance(archive.extra_data, dict) else {}
        glo = meta.get("gcode_label_objects")
        eo = meta.get("exclude_object")
        client.state.skip_objects_supported = bool(glo) and bool(eo)

        logger.info(
            "Loaded %s printable objects for printer %s from archive %s (skip_objects_supported=%s)",
            len(printable_objects),
            printer_id,
            archive.id,
            client.state.skip_objects_supported,
        )
        return True
    except Exception as e:
        logger.debug("Failed to extract printable objects from archive %s: %s", archive.id, e)
        return False


# Sorts over what the print actually consumed. Kept as a table rather than an
# if/elif chain because the route validates against the same keys — one place
# to add a column, and no way for the two to drift.
#
# ⚠️ ``actual_time_seconds``, not ``print_time_seconds``: the first is what the
# print really took, the second is the slicer's estimate. Sorting a farm's
# history by "longest print" should answer with reality.
_MEASURED_SORTS: dict[str, tuple[object, str]] = {
    "cost-desc": (PrintArchive.cost, "desc"),
    "cost-asc": (PrintArchive.cost, "asc"),
    "energy-desc": (PrintArchive.energy_kwh, "desc"),
    "energy-asc": (PrintArchive.energy_kwh, "asc"),
    "filament-desc": (PrintArchive.filament_used_grams, "desc"),
    "filament-asc": (PrintArchive.filament_used_grams, "asc"),
    "duration-desc": (PrintArchive.actual_time_seconds, "desc"),
    "duration-asc": (PrintArchive.actual_time_seconds, "asc"),
}


class ArchiveService:
    """Service for archiving print jobs."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def compute_file_hash(file_path: Path) -> str:
        """Compute SHA256 hash of a file for duplicate detection."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            # Read in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def get_duplicate_hashes_and_names(self) -> tuple[set[str], set[tuple[str, str]]]:
        """Get all effective hashes and (print name, hash) pairs that appear more than once.

        Uses ``COALESCE(source_content_hash, content_hash)`` as the effective
        hash so BamDude-patched archives collapse against their original
        source. External prints (no source hash) fall back to raw content
        hash — same behaviour as before m009.

        Returns a tuple of (duplicate_hashes, duplicate_name_hash_pairs).
        """
        from sqlalchemy import func

        effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)

        # Trashed archives are excluded — a duplicate badge against a trashed
        # sibling is misleading; those rows are slated for hard-delete.
        result = await self.db.execute(
            select(effective_hash)
            .where(PrintArchive.content_hash.isnot(None), PrintArchive.deleted_at.is_(None))
            .group_by(effective_hash)
            .having(func.count(PrintArchive.id) > 1)
        )
        duplicate_hashes = {row[0] for row in result.all()}

        # Find print names that have multiple archives with the SAME effective hash
        # This avoids marking different files with the same name as duplicates
        result = await self.db.execute(
            select(func.lower(PrintArchive.print_name), effective_hash)
            .where(
                PrintArchive.print_name.isnot(None),
                PrintArchive.content_hash.isnot(None),
                PrintArchive.deleted_at.is_(None),
            )
            .group_by(func.lower(PrintArchive.print_name), effective_hash)
            .having(func.count(PrintArchive.id) > 1)
        )
        duplicate_name_hash_pairs = {(row[0], row[1]) for row in result.all()}

        return duplicate_hashes, duplicate_name_hash_pairs

    async def find_duplicates(
        self,
        archive_id: int,
        content_hash: str | None = None,
        print_name: str | None = None,
        makerworld_model_id: str | None = None,
    ) -> list[dict]:
        """Find duplicate archives based on hash or name matching.

        Returns list of dicts with id, print_name, created_at, match_type.
        """
        duplicates = []

        # First, find exact matches by effective hash (source_content_hash or
        # content_hash). This groups BamDude-patched archives with their
        # library originals; external prints still dedup by raw content_hash.
        if content_hash:
            effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
            result = await self.db.execute(
                select(PrintArchive)
                .where(
                    and_(
                        effective_hash == content_hash,
                        PrintArchive.id != archive_id,
                        PrintArchive.deleted_at.is_(None),
                    )
                )
                .order_by(PrintArchive.created_at.desc())
                .limit(10)
            )
            for archive in result.scalars().all():
                duplicates.append(
                    {
                        "id": archive.id,
                        "print_name": archive.print_name,
                        "created_at": archive.created_at,
                        "match_type": "exact",
                    }
                )

        # Then, find similar matches by print name or MakerWorld ID
        # Prefer strict name+hash matching when hash exists; fallback to name-only for legacy/manual
        # archives that may not have a content_hash.
        if print_name or makerworld_model_id:
            conditions = [PrintArchive.id != archive_id, PrintArchive.deleted_at.is_(None)]

            name_conditions = []
            if print_name:
                if content_hash:
                    # Match if print names are similar AND share the same effective hash
                    # (chain-coalesced so a patched re-print of the same library file is
                    # treated as the same file even though its raw content_hash differs).
                    name_conditions.append(
                        and_(
                            PrintArchive.print_name.ilike(print_name),
                            func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash) == content_hash,
                        )
                    )
                else:
                    # Fallback for archives without hash data: match by print name only.
                    name_conditions.append(PrintArchive.print_name.ilike(print_name))
            if makerworld_model_id:
                # Match by MakerWorld model ID stored in extra_data (same design from MakerWorld)
                # Use json_extract for SQLite compatibility (astext is PostgreSQL-only)
                name_conditions.append(
                    func.json_extract(PrintArchive.extra_data, "$.makerworld_model_id") == str(makerworld_model_id)
                )

            if name_conditions:
                conditions.append(or_(*name_conditions))

                result = await self.db.execute(
                    select(PrintArchive).where(and_(*conditions)).order_by(PrintArchive.created_at.desc()).limit(10)
                )
                for archive in result.scalars().all():
                    # Don't add if already in duplicates (exact match)
                    if not any(d["id"] == archive.id for d in duplicates):
                        duplicates.append(
                            {
                                "id": archive.id,
                                "print_name": archive.print_name,
                                "created_at": archive.created_at,
                                "match_type": "similar",
                            }
                        )

        return duplicates

    async def _default_rate_cost(self, filament_grams: float | None) -> float | None:
        """What this much filament costs at the farm's rate, or None.

        None when the operator has never set a rate: the alternative is 0.00,
        which reads as "this print was free" rather than "nobody said what
        filament costs here". See ``services/filament_cost``.
        """
        from backend.app.services.filament_cost import cost_of, default_rate_per_kg

        return cost_of(filament_grams, await default_rate_per_kg(self.db))

    async def archive_print(
        self,
        printer_id: int | None,
        source_file: Path,
        print_data: dict | None = None,
        created_by_id: int | None = None,
        original_filename: str | None = None,
        project_id: int | None = None,
        *,
        project_line_id: int | None = None,
        source_content_hash: str | None = None,
        applied_patches: list[str] | None = None,
        subtask_id: str | None = None,
        library_file_id: int | None = None,
        swap_macro_events_pending: list[str] | None = None,
        selected_macro_ids: list[int] | None = None,
        prefer_filename_for_name: bool = False,
        plate_index: int | None = None,
        dispatched_file: Path | None = None,
        stored_filename: str | None = None,
        is_calibration: bool = False,
        calibration_session_id: int | None = None,
        dispatch_intent: dict[str, object] | None = None,
    ) -> PrintArchive | None:
        """Archive a 3MF file with metadata.

        Args:
            printer_id: ID of the printer (optional)
            source_file: Path to the original (unpatched) 3MF — chain root.
                Used for ``source_content_hash`` (when not passed explicitly),
                for filename / stem / suffix derivation, and for fallback when
                ``dispatched_file`` is not provided.
            dispatched_file: Path to the EXACT bytes that went to the printer
                (post-patcher). When provided this controls what gets hashed
                into ``content_hash`` and copied into the archive folder, so
                restart-recovery in ``on_print_start`` can match the archive
                by hashing the printer's copy. ``None`` means no patching
                happened and ``content_hash == source_content_hash`` —
                identical to the legacy single-file flow.
            print_data: Print data from MQTT (optional)
            created_by_id: User ID who created this archive (optional, for user tracking)
            original_filename: Original human-readable filename (optional, for library files
                stored with UUID names)
            source_content_hash: SHA256 of the UNPATCHED source file, when the
                caller (BamDude dispatch) knows it. None for external prints.
            stored_filename: Name to keep the archived copy under, when
                ``source_file.name`` is not a name anybody should read. Since m173
                a dispatch's ``source_file`` is the captured object in the queue
                spool, whose name is its sha256 — and the archive folder around it
                is named after the human stem, so without this one tree would hold
                both conventions and the person reading it during an incident pays.
                Containment-checked like ``attach_3mf_to_archive``'s ``dest_name``;
                ``None`` keeps the source file's own name, as every caller before
                m173 did.
            applied_patches: Patch identifiers applied by the dispatch pipeline
                before upload. None for external prints.
            subtask_id: Printer-assigned subtask identifier from MQTT push_status,
                captured by on_print_start when available. Advisory pre-check
                key in later resume attempts (#972).
            library_file_id: ID of the ``library_files`` row this print was
                dispatched from, when BamDude drove the dispatch. None for
                external / direct SD / reprint-from-archive paths; m014 later
                backfills those by hash where possible.
            prefer_filename_for_name: When True, use the uploaded filename stem as
                the archive's display name even if the 3MF embeds a `print_name`
                in its metadata. Used by virtual-printer flows so users who rename
                a job in BambuStudio's "send to printer" dialog see that name
                instead of the creator-baked title (#1152, audit B.14).
            dispatch_intent: BamDude's versioned, pre-publish identity for an
                owned dispatch.  It is intentionally distinct from the
                printer-observed ``subtask_id`` column.
        """
        # Verify printer exists if specified
        if printer_id is not None:
            result = await self.db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if not printer:
                return None

        # Two distinct hashes, two distinct purposes:
        # - ``content_hash`` = SHA256 of the *dispatched* bytes (post-patch,
        #   what FTP sent to the printer). Drives ``on_print_start``'s
        #   restart-recovery query: it pulls the printer's copy back over
        #   FTP, hashes it, and looks for ``content_hash == temp_hash``.
        #   Must be the patched hash for that to match.
        # - ``source_content_hash`` (set further down via ``chain_lookup``
        #   or as ``content_hash`` when the row is its own chain root) =
        #   SHA256 of the unpatched original. Drives chain-of-custody
        #   grouping and file-on-disk dedup.
        # On disk we keep the ORIGINAL (unpatched) bytes — that way reprint
        # can re-run the patcher against a clean source and toggle
        # mesh_mode_fast_check / gcode injection in either direction. The
        # patcher's M970 regex only matches *uncommented* lines, so a
        # patched-on-disk source could never have an earlier patch undone.
        bytes_for_disk: Path = source_file
        hashed_bytes: Path = dispatched_file if dispatched_file is not None else source_file
        content_hash = self.compute_file_hash(hashed_bytes)

        # External-print fallback: if the caller didn't provide source_content_hash
        # (i.e. print was initiated outside BamDude), try to link this archive to
        # an existing chain by looking for any prior archive that has either the
        # same content_hash (exact bytes on disk) or the same source_content_hash
        # (this file is itself the original someone patched before). The oldest
        # match wins — it's closest to the root of the chain.
        if source_content_hash is None:
            # Trashed archives are excluded — a chain anchor in the trash is
            # about to be hard-deleted, so reusing its hash would orphan us.
            chain_lookup = await self.db.execute(
                select(func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash))
                .where(
                    or_(
                        PrintArchive.content_hash == content_hash,
                        PrintArchive.source_content_hash == content_hash,
                    ),
                    PrintArchive.deleted_at.is_(None),
                )
                .order_by(PrintArchive.created_at.asc())
                .limit(1)
            )
            chain_hash = chain_lookup.scalar_one_or_none()
            if chain_hash:
                # Always inherit a chain hash when one exists, even when it
                # equals our content_hash — keeps the always-fill invariant
                # consistent regardless of whether a previous variant was
                # patched or unpatched.
                source_content_hash = chain_hash
            else:
                # Standalone-row case: no existing archive shares this hash.
                # Set source = content so this row becomes the chain root for
                # any future patched variant. Always-fill invariant: every
                # row written by this code path has source_content_hash set.
                source_content_hash = content_hash

        # File-on-disk dedup is on the chain-root hash (``effective_hash =
        # COALESCE(source_content_hash, content_hash)``) — every row that
        # shares the same unpatched origin shares the same on-disk file,
        # because that's exactly what we now write to disk regardless of
        # which patches the dispatcher applied. Two patched variants and
        # the unpatched original of the same source all collapse to one
        # disk copy. Cross-printer share works for free for the same
        # reason. ``delete_archive`` ref-counts shared ``file_path``s;
        # oldest match wins to keep the on-disk anchor stable.
        printer_folder = str(printer_id) if printer_id is not None else "unassigned"
        effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
        existing = await self.db.execute(
            select(PrintArchive)
            .where(
                effective_hash == source_content_hash,
                PrintArchive.file_path.isnot(None),
                PrintArchive.file_path != "",
                PrintArchive.deleted_at.is_(None),
            )
            .order_by(PrintArchive.created_at.asc())
            .limit(1)
        )
        existing_archive = existing.scalar_one_or_none()

        # `display_stem` is used below as a fallback for `print_name` when the
        # 3MF has no name metadata. Hoist it out of the if/else so the reuse
        # path (existing_archive) also has a valid value.
        display_stem = resolve_display_stem(original_filename if original_filename else source_file.name)

        if existing_archive and existing_archive.file_path:
            # Reuse existing file on disk
            dest_file = settings.base_dir / existing_archive.file_path
            archive_dir = dest_file.parent
            thumbnail_reuse = existing_archive.thumbnail_path
        else:
            # Create new archive directory and copy file. Belt-and-suspenders
            # uniqueness: timestamp is per-second, so theoretically a same-name
            # different-content print on the same printer in the same second
            # could land in an existing dir and overwrite it. In practice the
            # printer can't run two prints at once, but the suffix loop costs
            # nothing and removes the silent-overwrite footgun.
            archive_dir = (
                create_archive_directory(  # SEC-PATH-OK: printer_folder is server-owned printer id or "unassigned".
                    settings.archive_dir / printer_folder, display_stem
                )
            )
            # The human name when the caller has one (a captured source's own name
            # is its hash — see ``stored_filename``), containment-checked exactly
            # as ``attach_3mf_to_archive`` does it: the string reaches here from a
            # row or from a job's ``source_snapshot``, neither of which this
            # function gets to trust with a path component.
            dest_file = (
                safe_join_under(archive_dir, stored_filename, http=False)
                if stored_filename
                else archive_dir / source_file.name
            )
            # Explicit fsync'd loop avoids the shutil.copy2 → sendfile short-read
            # quirk that silently truncated 3MF archives on some platforms (#1032).
            # ``bytes_for_disk`` is the post-patch file when the dispatcher
            # passed ``dispatched_file``; otherwise it's ``source_file``.
            _copy_and_fsync(bytes_for_disk, dest_file)

            # Verify the dest is a valid ZIP before going any further. Staying
            # quiet here is how #1032 escaped review — the archive row was
            # written but every later zipfile.ZipFile() call on the dest failed
            # with "File is not a zip file".
            if (
                source_file.suffix.lower() == ".3mf"
                and zipfile.is_zipfile(bytes_for_disk)
                and not zipfile.is_zipfile(dest_file)
            ):
                try:
                    src_size = bytes_for_disk.stat().st_size
                    dst_size = dest_file.stat().st_size
                except OSError:
                    src_size = dst_size = -1
                logger.error(
                    "Archive copy corrupted 3MF: src=%s (%s bytes, valid ZIP) -> dst=%s "
                    "(%s bytes, NOT a ZIP). Refusing to create archive row.",
                    bytes_for_disk,
                    src_size,
                    dest_file,
                    dst_size,
                )
                # Narrow cleanup: remove only the truncated file and the archive
                # directory if it's now empty. The dir is freshly created (the
                # suffix loop above guarantees no pre-existing collision), so
                # rmdir is safe — but keep it gated on emptiness as defence in
                # depth in case any future caller adds files before this point.
                try:
                    dest_file.unlink()
                except OSError:
                    pass
                try:
                    archive_dir.rmdir()
                except OSError:
                    pass  # directory not empty — leave untouched
                return None

            thumbnail_reuse = None

        # Extract plate number from filename (e.g., "plate_5" from "/data/Metadata/plate_5.gcode")
        plate_number = None
        if print_data:
            filename = print_data.get("filename", "")
            match = re.search(r"plate_(\d+)", filename)
            if match:
                plate_number = int(match.group(1))

        # Resolve "which plate ran" BEFORE parsing so the parser's
        # thumbnail extraction + slice_info filtering both align with
        # the actually-printed plate. Priority:
        #   1. Caller-supplied (queue item / dispatch options) — what
        #      the user picked before the print started.
        #   2. ``plate_number`` from the printer's gcode filename
        #      ("Metadata/plate_N.gcode") — what MQTT saw on the wire.
        #   3. After parse(): ``parser.plate_number`` — slice_info
        #      single-plate-export fallback when neither (1) nor (2)
        #      was available.
        # The thumbnail + per-plate slice_info both honour the value
        # passed to the constructor, so a multi-plate container of
        # which plate 5 was printed produces an archive with plate 5's
        # thumbnail, print_time, weight, and per-slot filament usage —
        # not plate 1's.
        plate_for_parser = plate_index or plate_number

        # Parse 3MF metadata
        parser = ThreeMFParser(dest_file, plate_number=plate_for_parser)
        metadata = parser.parse()

        resolved_plate_index = plate_for_parser or parser.plate_number

        # Per-plate cache so the gallery / list endpoint doesn't reopen the
        # ZIP every time. Same idea as the library upload route.
        try:
            with zipfile.ZipFile(dest_file, "r") as _zfh:
                plates_payload = parse_plates_from_3mf(_zfh)
            if plates_payload:
                metadata["plates"] = plates_payload
                metadata["is_multi_plate"] = len(plates_payload) > 1
        except Exception as _pe:
            logger.debug("archive_print: per-plate parse failed (non-critical): %s", _pe)

        # Save thumbnail if present (reuse existing if file was deduped)
        thumbnail_path = thumbnail_reuse
        if "_thumbnail_data" in metadata:
            if not thumbnail_reuse:
                thumb_file = archive_dir / f"thumbnail{metadata['_thumbnail_ext']}"
                thumb_file.write_bytes(metadata["_thumbnail_data"])
                thumbnail_path = str(thumb_file.relative_to(settings.base_dir))
            del metadata["_thumbnail_data"]
            del metadata["_thumbnail_ext"]

        # Merge with print data from MQTT
        if print_data:
            metadata["_print_data"] = print_data

        # The submitted wire identity is durable evidence of what BamDude
        # intended to start, not an echo from the printer.  Keeping it in a
        # private versioned namespace means later 3MF attach metadata merges
        # preserve it without conflating it with ``PrintArchive.subtask_id``.
        if dispatch_intent:
            metadata["dispatch_intent"] = dict(dispatch_intent)

        # Persist swap-macro intent in the same INSERT as the rest of
        # metadata. ``on_print_complete`` reads this when its in-memory
        # ``_active_swap_config`` is empty (post-restart recovery) and
        # clears the key after firing the macro to keep idempotency.
        # Only meaningful when ``swap_mode_change_table`` is in the list
        # (the only event ``on_print_complete`` consults). Pre-stamping
        # at archive creation avoids a separate post-start_print UPDATE
        # that races the runtime-tracker write loop on SQLite's single
        # writer and times out under busy_timeout.
        if swap_macro_events_pending and "swap_mode_change_table" in swap_macro_events_pending:
            metadata["swap_macro_events_pending"] = list(swap_macro_events_pending)
        # Unlike the swap line above this records whatever was chosen, empty
        # list included: "the operator ticked nothing" is an answer the
        # triggers must be able to read back after a restart.
        if selected_macro_ids is not None:
            metadata["selected_macro_ids"] = [int(i) for i in selected_macro_ids]

        # Determine status and timestamps. Default `'completed'` covers the
        # path where on_print_complete archives a finished print without
        # passing print_data (rare, but defensive). The legacy `'archived'`
        # status (pre-0.4.2 "uploaded but never printed" rows from the
        # now-removed manual-upload / VP-placeholder / pending-uploads flows)
        # is no longer produced anywhere; only legacy DBs may still carry such
        # rows, which the stats / history queries defensively exclude.
        status = print_data.get("status", "completed") if print_data else "completed"
        started_at = datetime.now(timezone.utc) if status == "printing" else None
        completed_at = datetime.now(timezone.utc) if status in ("completed", "failed") else None

        # Calculate initial cost estimate from the farm-wide rate.
        # This is a placeholder - usage_tracker.on_print_complete() will overwrite
        # archive.cost with the actual cost from spool.cost_per_kg later. With no
        # rate set it stays None rather than becoming 0.00, which would claim the
        # print was free.
        cost = await self._default_rate_cost(metadata.get("filament_used_grams"))

        # Calculate quantity from printable objects count
        # printable_objects is a dict of {identify_id: name} for non-skipped objects
        quantity = 1  # Default to 1
        printable_objects = metadata.get("printable_objects")
        if printable_objects and isinstance(printable_objects, dict):
            quantity = len(printable_objects)
            logger.debug("Auto-detected %s parts from 3MF printable objects", quantity)

        # Mirror the resolved plate index into ``extra_data['plate_id']`` so
        # the existing reader in ``queue_virtual.py`` finds it for VP-recreate
        # flows. Column is the source of truth; the JSON copy keeps the
        # legacy contract working without an extra reader rewrite.
        if resolved_plate_index is not None:
            metadata["plate_id"] = resolved_plate_index

        # Swap compatibility, decided at creation. ``on_print_complete`` reads
        # only the archive's flag to auto-clear the plate gate, and the
        # dispatch path creates its archive here — so without this a
        # swap_compatible library file printed from the queue completed with
        # swap_compatible=False and armed the gate anyway. Library flag first
        # (immutable, set at upload), then the same name check the fallback
        # and attach paths use.
        swap_compatible = False
        if library_file_id is not None:
            from backend.app.models.library import LibraryFile

            swap_compatible = bool(
                await self.db.scalar(select(LibraryFile.swap_compatible).where(LibraryFile.id == library_file_id))
            )
        if not swap_compatible:
            fname_lower = (original_filename or source_file.name).lower()
            swap_compatible = (
                fname_lower.endswith((".swap.3mf", ".swaps.3mf")) or ".swap." in fname_lower or ".swaps." in fname_lower
            )

        # Create archive record
        archive = PrintArchive(
            printer_id=printer_id,
            filename=original_filename or source_file.name,
            file_path=str(dest_file.relative_to(settings.base_dir)),
            file_size=dest_file.stat().st_size,
            content_hash=content_hash,
            source_content_hash=source_content_hash,
            applied_patches=json.dumps(applied_patches) if applied_patches else None,
            thumbnail_path=thumbnail_path,
            print_name=display_stem if prefer_filename_for_name else (metadata.get("print_name") or display_stem),
            print_time_seconds=metadata.get("print_time_seconds"),
            filament_used_grams=metadata.get("filament_used_grams"),
            filament_type=metadata.get("filament_type"),
            filament_color=metadata.get("filament_color"),
            layer_height=metadata.get("layer_height"),
            total_layers=metadata.get("total_layers"),
            nozzle_diameter=metadata.get("nozzle_diameter"),
            bed_temperature=metadata.get("bed_temperature"),
            bed_type=metadata.get("bed_type"),
            nozzle_temperature=metadata.get("nozzle_temperature"),
            sliced_for_model=metadata.get("sliced_for_model"),
            plate_index=resolved_plate_index,
            makerworld_url=metadata.get("makerworld_url"),
            designer=metadata.get("designer"),
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            cost=cost,
            quantity=quantity,
            extra_data=metadata,
            skip_objects_supported=skip_objects_supported_from_metadata(metadata),
            created_by_id=created_by_id,
            project_id=project_id,
            project_line_id=project_line_id,
            subtask_id=subtask_id,
            library_file_id=library_file_id,
            swap_compatible=swap_compatible,
            is_calibration=is_calibration,
            calibration_session_id=calibration_session_id,
        )

        self.db.add(archive)
        await self.db.flush()

        # Seed the per-part plate state (m158). Source bytes, not dispatched:
        # the patcher touches gcode, never slice_info, and the on-disk copy is
        # the unpatched original anyway. Pass the Path, not the bytes — the
        # read happens inside seed_archive_parts' own guard so a transient
        # read error can never raise out of archive creation.
        await seed_archive_parts(self.db, archive, source_file)

        await self.db.commit()
        await self.db.refresh(archive)

        return archive

    async def attach_3mf_to_archive(
        self,
        archive_id: int,
        source_file: Path,
        original_filename: str | None = None,
    ) -> bool:
        """Prepare a recovered 3MF off-loop, then publish it under a short guard.

        Recovery callers already finish their start publication before this
        method. The authoritative row is read again just before publication;
        preparation never carries a live ORM object across the wait.
        """
        from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

        # A live plate correction between snapshot and publication needs a
        # fresh off-lock parse. One retry covers the real correction race; a
        # continuously changing row is left untouched rather than publishing
        # metadata for an indeterminate plate.
        for attempt in range(2):
            prepared: _PreparedArchiveAttach | None = None
            try:
                # Snapshot only the filesystem inputs needed for preparation.
                # This deliberately opens/releases the writer before hashing,
                # copying and parsing a potentially huge G-code container.
                async with archive_write_scope(self.db, archive_id):
                    archive = await load_active_archive_for_write(self.db, archive_id)
                    if archive is None:
                        await self.db.rollback()
                        return False
                    printer_folder = str(archive.printer_id) if archive.printer_id is not None else "unassigned"
                    plate_index = archive.plate_index
                    await self.db.commit()

                prepare_task = asyncio.create_task(
                    asyncio.to_thread(
                        _prepare_archive_attach_file,
                        source_file,
                        original_filename,
                        settings.archive_dir,
                        printer_folder,
                        plate_index,
                    )
                )
                try:
                    # Cancelling the coroutine cannot synchronously stop a
                    # filesystem thread. Shield it, then arrange exact staging
                    # cleanup when the worker reaches its natural boundary.
                    prepared = await asyncio.shield(prepare_task)
                except asyncio.CancelledError:

                    def _discard_after_prepare(task: asyncio.Task[_PreparedArchiveAttach]) -> None:
                        try:
                            late_prepared = task.result()
                        except BaseException:
                            return
                        asyncio.create_task(asyncio.to_thread(_discard_prepared_archive_attach, late_prepared))

                    prepare_task.add_done_callback(_discard_after_prepare)
                    raise
                async with archive_write_scope(self.db, archive_id):
                    return await self._attach_3mf_to_archive_locked(
                        archive_id,
                        source_file,
                        original_filename,
                        prepared=prepared,
                    )
            except _AttachPreparationStale:
                await self.db.rollback()
                logger.info("attach preparation became stale for archive %s (retry %s/2)", archive_id, attempt + 1)
            except asyncio.CancelledError:
                # Cancellation is not an attach failure. Roll back only our
                # open DB work, discard our private staging tree and let the
                # caller decide whether/when to retry.
                await self.db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 - public attach keeps its bool contract
                await self.db.rollback()
                logger.exception("attach_3mf_to_archive preparation failed for archive %s: %s", archive_id, exc)
                return False
            finally:
                if prepared is not None:
                    await asyncio.to_thread(_discard_prepared_archive_attach, prepared)
        return False

    async def _attach_3mf_to_archive_locked(
        self,
        archive_id: int,
        source_file: Path,
        original_filename: str | None = None,
        *,
        prepared: _PreparedArchiveAttach | None = None,
    ) -> bool:
        """Fill in an empty/fallback archive with a 3MF that was recovered
        later (e.g. by the background download-retry service).

        Unlike :meth:`archive_print`, this updates an existing row in place
        instead of inserting a new one.  Use case: ``on_print_start`` could
        not download the 3MF at the time, so a fallback row was created
        with ``file_path=""`` + ``extra_data["no_3mf_available"]=True``;
        the retry service later manages to grab the file from SD.

        Does NOT touch ``status``, ``completed_at``, ``project_id``,
        ``project_line_id``, or ``created_by_id`` — those were set when the
        archive was originally created; never **overwrites** ``started_at`` —
        fills it only for a row adopted mid-flight
        (``extra_data['recovered_start']``, spec 2026-09-12 §3.3) that has none.

        Returns True on success, False on parse/copy failure.
        """
        created_archive_dir: Path | None = None
        published_file_path: str | None = None
        file_reference_scope = None
        committed = False
        try:
            from backend.app.services.archive_write_scope import load_active_archive_for_write

            archive = await load_active_archive_for_write(self.db, archive_id)
            if archive is None:
                await self.db.rollback()
                return False
            if prepared is not None and archive.plate_index != prepared.requested_plate_index:
                # Do not attach old-plate metadata just because the bytes are
                # unchanged. The public wrapper releases this short guard,
                # re-prepares for the fresh plate and tries once more.
                raise _AttachPreparationStale()
            content_hash = prepared.content_hash if prepared is not None else self.compute_file_hash(source_file)
            if archive.file_path and archive.content_hash == content_hash:
                # A retry that crossed a successful attach must be a no-op,
                # but a later correction may deliberately change the archive's
                # plate while keeping the same 3MF bytes.  ``_attached_plate``
                # records which plate produced the currently stored metadata;
                # legacy rows have no proof and therefore take the safe
                # re-attach path.
                attached_plate = (archive.extra_data or {}).get("_attached_plate_index")
                if attached_plate == archive.plate_index:
                    await self.db.commit()
                    return True

            # Inherit chain root from any existing archive with matching
            # content/source hash — mirrors archive_print's chain_lookup.
            # Fallback archives are created with source_content_hash=NULL
            # (no dispatch context), so without this they'd never link to
            # the chain that already exists for the same source file.
            if archive.source_content_hash is None:
                chain_lookup = await self.db.execute(
                    select(func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash))
                    .where(
                        or_(
                            PrintArchive.content_hash == content_hash,
                            PrintArchive.source_content_hash == content_hash,
                        ),
                        PrintArchive.id != archive_id,
                        PrintArchive.deleted_at.is_(None),
                    )
                    .order_by(PrintArchive.created_at.asc())
                    .limit(1)
                )
                chain_hash = chain_lookup.scalar_one_or_none()
                # Always-fill invariant: source_content_hash is never NULL
                # for rows written by this code path. Inherit chain root if
                # any sibling exists, else seed with our own content_hash.
                archive.source_content_hash = chain_hash or content_hash

            printer_folder = str(archive.printer_id) if archive.printer_id is not None else "unassigned"
            display_stem = (
                prepared.display_stem
                if prepared is not None
                else resolve_display_stem(original_filename if original_filename else source_file.name)
            )
            dest_name = prepared.dest_name if prepared is not None else original_filename or source_file.name

            # Reuse the chain's on-disk file when one exists. Match on the
            # chain-root hash (``effective_hash = COALESCE(source_content_hash,
            # content_hash)``) — every row that shares an unpatched origin
            # also shares the same on-disk file because ``archive_print``
            # writes the unpatched source there. The bytes we just hashed
            # from the printer (``source_file``) are post-patch and would
            # disagree with what's on disk, but that's exactly the point:
            # ``content_hash`` stays as the FTP/restart-recovery key, while
            # ``file_path`` always points at the unpatched copy reprint can
            # safely re-feed to the patcher.
            effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
            existing_with_file = await self.db.execute(
                select(PrintArchive)
                .where(
                    effective_hash == archive.source_content_hash,
                    PrintArchive.id != archive_id,
                    PrintArchive.file_path.isnot(None),
                    PrintArchive.file_path != "",
                    PrintArchive.deleted_at.is_(None),
                )
                .order_by(PrintArchive.created_at.asc())
                .limit(1)
            )
            existing_archive = existing_with_file.scalar_one_or_none()

            if existing_archive and existing_archive.file_path:
                from backend.app.services.archive_write_scope import archive_file_reference_scope

                file_reference_scope = archive_file_reference_scope(self.db, existing_archive.file_path)
                await file_reference_scope.__aenter__()
                # Reuse existing on-disk file. ``delete_archive`` ref-counts
                # shared paths so the file stays as long as any row refs it.
                dest_file = settings.base_dir / existing_archive.file_path
                if dest_file.is_file():
                    archive_dir = dest_file.parent
                    thumbnail_reuse = existing_archive.thumbnail_path
                else:
                    # A stale DB reference is not a reusable donor. Publishing
                    # that path would report a successful recovery while the
                    # new archive still has no bytes; make a new durable copy
                    # from this invocation's prepared source instead.
                    logger.warning(
                        "attach_3mf_to_archive donor archive %s names missing file %s; copying fresh bytes",
                        existing_archive.id,
                        existing_archive.file_path,
                    )
                    existing_archive = None
            if existing_archive is None:
                # No existing copy — create a fresh archive_dir and copy
                # from the temp source. Suffix loop guards the
                # theoretical-only same-second collision.
                archive_dir = (
                    create_archive_directory(  # SEC-PATH-OK: printer_folder is server-owned printer id or "unassigned".
                        settings.archive_dir / printer_folder, display_stem
                    )
                )
                created_archive_dir = archive_dir
                # Prefer the clean original_filename (e.g. "Swapmod_STL.gcode.3mf")
                # over the potentially-prefixed temp source_file name (e.g.
                # "cover_1_Swapmod_STL.gcode.3mf" when it came from the cover
                # endpoint's temp download).
                # dest_name can originate from a printer FTP listing / MQTT
                # subtask name (not a request), so containment-check the join.
                dest_file = safe_join_under(archive_dir, dest_name, http=False)
                if prepared is not None:
                    # The fsync'd copy and ZIP validation happened outside the
                    # writer. Both paths are under archive_dir, so rename is a
                    # small same-filesystem publication step rather than a
                    # second large copy while SQLite is exclusively locked.
                    os.replace(prepared.staged_file, dest_file)
                else:
                    # Compatibility path for private/direct callers that have
                    # not yet been moved through the preparation wrapper.
                    _copy_and_fsync(source_file, dest_file)
                    if (
                        source_file.suffix.lower() == ".3mf"
                        and zipfile.is_zipfile(source_file)
                        and not zipfile.is_zipfile(dest_file)
                    ):
                        raise ValueError("copied 3MF is not a valid ZIP")
                thumbnail_reuse = None

            # Parse 3MF metadata (reuse the same parser as archive_print).
            # Pass the archive's recorded plate_index so per-plate
            # thumbnail + slice_info come from the actually-printed
            # plate, not whatever happened to be plate 1 in the
            # container. None for legacy/external rows where the index
            # is unknown — parser falls back to slice_info's first
            # plate, matching pre-m038 behaviour.
            if prepared is not None:
                metadata = dict(prepared.metadata)
                parser_plate_number = prepared.parser_plate_index
            else:
                parser = ThreeMFParser(dest_file, plate_number=archive.plate_index)
                metadata = parser.parse()
                parser_plate_number = parser.plate_number

            # A row can legitimately arrive here without a plate index: nothing
            # in ``Metadata/plate_N.gcode`` to parse for a single-plate export.
            # ``archive_print`` resolves that same case from the parser's own
            # slice_info fallback, and every plate-scoped reader downstream
            # (cover, gcode tab, skip-objects, Spoolman) keys off this column —
            # so leaving it NULL for ever is not neutral, it silently sends
            # them all back to "whatever plate 1 happens to be".
            # ⚠️ Backfill only. A value already on the row came from the live
            # MQTT state — what the printer is actually running — and the
            # container, which holds several plates, cannot overrule that.
            if archive.plate_index is None and parser_plate_number is not None:
                archive.plate_index = parser_plate_number

            # Per-plate cache populated alongside the rest of the metadata.
            if prepared is not None:
                plates_payload = prepared.plates_payload
                if plates_payload:
                    metadata["plates"] = plates_payload
                    metadata["is_multi_plate"] = len(plates_payload) > 1
            else:
                try:
                    with zipfile.ZipFile(dest_file, "r") as _zfh:
                        plates_payload = parse_plates_from_3mf(_zfh)
                    if plates_payload:
                        metadata["plates"] = plates_payload
                        metadata["is_multi_plate"] = len(plates_payload) > 1
                except Exception as _pe:
                    logger.debug("attach_3mf_to_archive: per-plate parse failed (non-critical): %s", _pe)

            thumbnail_path = None
            if thumbnail_reuse:
                # File-share branch: reuse the existing archive's thumbnail
                # path. It lives in the same shared directory so it's
                # already on disk and ref-counted via delete_archive's
                # file_path share check (the dir as a whole is kept while
                # any row points into it).
                thumbnail_path = thumbnail_reuse
                metadata.pop("_thumbnail_data", None)
                metadata.pop("_thumbnail_ext", None)
            elif "_thumbnail_data" in metadata:
                thumb_file = archive_dir / f"thumbnail{metadata['_thumbnail_ext']}"
                thumb_file.write_bytes(metadata["_thumbnail_data"])
                thumbnail_path = str(thumb_file.relative_to(settings.base_dir))
                del metadata["_thumbnail_data"]
                del metadata["_thumbnail_ext"]

            # Merge metadata into existing extra_data.  Preserve _print_data
            # (set at fallback creation with the MQTT start payload) and
            # drop the retry / no-3mf flags now that we have the file.
            merged_extra = dict(archive.extra_data or {})
            preserved_print_data = merged_extra.get("_print_data")
            merged_extra.update(metadata)
            if preserved_print_data is not None:
                merged_extra["_print_data"] = preserved_print_data
            merged_extra.pop("no_3mf_available", None)
            merged_extra.pop("no_3mf_reason", None)
            merged_extra.pop("download_retry_count", None)
            merged_extra.pop("download_next_retry", None)
            # Mirror of the column, for the legacy reader in ``queue_virtual.py``
            # — ``archive_print`` writes it, so a row filled in through this
            # path has to as well or VP-recreate loses the plate.
            if archive.plate_index is not None:
                merged_extra["plate_id"] = archive.plate_index
            # This is deliberately distinct from ``plate_id``.  The latter is
            # the current print's selected plate; this one proves which plate
            # the attached metadata/file snapshot describes, so a later
            # correction with the same bytes can re-attach instead of being
            # mistaken for a duplicate retry.
            merged_extra["_attached_plate_index"] = archive.plate_index

            archive.filename = original_filename or source_file.name
            archive.file_path = str(dest_file.relative_to(settings.base_dir))
            published_file_path = archive.file_path
            archive.file_size = dest_file.stat().st_size
            archive.content_hash = content_hash
            archive.thumbnail_path = thumbnail_path
            archive.print_name = metadata.get("print_name") or display_stem
            archive.print_time_seconds = metadata.get("print_time_seconds")
            archive.filament_used_grams = metadata.get("filament_used_grams")
            archive.filament_type = metadata.get("filament_type")
            archive.filament_color = metadata.get("filament_color")
            archive.layer_height = metadata.get("layer_height")
            archive.total_layers = metadata.get("total_layers")
            archive.nozzle_diameter = metadata.get("nozzle_diameter")
            archive.bed_temperature = metadata.get("bed_temperature")
            archive.bed_type = metadata.get("bed_type")
            archive.nozzle_temperature = metadata.get("nozzle_temperature")
            archive.sliced_for_model = metadata.get("sliced_for_model")
            archive.makerworld_url = metadata.get("makerworld_url")
            archive.designer = metadata.get("designer")
            archive.extra_data = merged_extra
            # The fallback row was created with no 3MF, so it defaulted to
            # False. Now that the file has landed we know the real answer.
            archive.skip_objects_supported = skip_objects_supported_from_metadata(metadata)

            # The estimate needed to place a mid-flight adoption's start time
            # only exists once the file has been parsed — here, and after
            # ``extra_data`` has taken its merged value, which the rule writes
            # its flag into. A no-op for every row that has a start.
            if _reconstruct_recovered_start(archive):
                logger.info(
                    "Reconstructed started_at for adopted archive %s: %s",
                    archive.id,
                    archive.started_at,
                )

            # Backfill cost + quantity — fallback creation seeded them with
            # NULL / 1, and without this the archive stays stuck there even
            # after the 3MF lands.  Mirrors the logic in archive_print().
            # Only when the row has no cost yet: a file that lands after the
            # print completed must not re-price what completion priced from the
            # spools that fed it (either inventory mode) at the farm rate
            # (upstream 39835437, #2591).
            filament_grams = metadata.get("filament_used_grams")
            if filament_grams and archive.cost is None:
                recovered = await self._default_rate_cost(filament_grams)
                if recovered is not None:
                    archive.cost = recovered

            printable_objects = metadata.get("printable_objects")
            if printable_objects and isinstance(printable_objects, dict):
                archive.quantity = len(printable_objects)

            # Swap-compatible detection by filename suffix — mirrors the
            # post-archive_print check in on_print_start. Fallback creation
            # defaulted swap_compatible=False, so without this backfill a
            # *.swap.3mf / *.swaps.3mf file landed via retry would stay
            # flagged as non-swap.
            fname_lower = (original_filename or source_file.name).lower()
            if fname_lower.endswith((".swap.3mf", ".swaps.3mf")) or ".swap." in fname_lower or ".swaps." in fname_lower:
                archive.swap_compatible = True

            # Link to the originating library file by content hash when we
            # didn't know it at fallback-creation time (on_print_start's FTP
            # miss path has no dispatch context). Mirrors m014's backfill
            # logic — oldest matching library row wins.
            if archive.library_file_id is None:
                from backend.app.models.library import LibraryFile

                match_hash = archive.source_content_hash or content_hash
                if match_hash:
                    lib_match = await self.db.execute(
                        select(LibraryFile.id)
                        .where(LibraryFile.file_hash == match_hash)
                        .order_by(LibraryFile.created_at.asc(), LibraryFile.id.asc())
                        .limit(1)
                    )
                    matched_id = lib_match.scalar_one_or_none()
                    if matched_id is not None:
                        archive.library_file_id = matched_id

            # The queue row for this print learns the plate the moment the
            # archive does. A print picked up from a slicer or the printer's
            # screen has no plate to know at ``on_print_start``: the printer
            # names the file ``<something>.gcode.3mf``, with no plate in it and
            # no ``gcode_file`` beside it, so the row is created blank. The
            # plate exists only once this method has parsed the container.
            #
            # It matters because that row is repeatable — and a repeat with no
            # plate prints plate 1 of a multi-plate file. Reported from a farm.
            #
            # ⚠️ Backfill only, mirroring the archive's own rule above: a value
            # already on the row was chosen by whoever dispatched or queued it,
            # and a container holding several plates cannot overrule that.
            # ⚠️ Keyed on ``archive_id`` — the link between this print and its
            # row. Keying on the printer would reach every row it ever ran.
            if archive.plate_index is not None:
                from backend.app.models.print_queue import PrintQueueItem

                await self.db.execute(
                    update(PrintQueueItem)
                    .where(PrintQueueItem.archive_id == archive_id)
                    .where(PrintQueueItem.plate_id.is_(None))
                    .values(plate_id=archive.plate_index)
                )

            # Seed the per-part plate state (m158) — same contract as
            # archive_print's call, now that archive.file_path points at
            # the freshly attached 3MF. Pass the Path — this whole method is
            # wrapped in a try/except that returns False on any failure, so
            # a read error here would otherwise fail the entire attach.
            await seed_archive_parts(
                self.db,
                archive,
                source_file if prepared is None else None,
                printable_objects=prepared.printable_objects if prepared is not None else None,
            )

            # ⚠️ The free-stock credit runs HERE too (Ruling 27), not only in
            # the completion hook. An external print reaches ``completed``
            # before its 3MF does — the hook fired against an archive with no
            # part rows at all and credited nothing, and the four retry triggers
            # that finally attach the file (startup sweep, reconnect, the
            # last-chance call in ``on_print_complete``, the manual button) only
            # seed the rows. Nothing re-ran the credit, so a print that arrived
            # by that door never reached the shelf.
            #
            # ``credit_if_unfiled`` is the never-fail wrapper the hook uses:
            # this whole method returns False on any exception, and losing an
            # attached 3MF over a bookkeeping refusal would be the worse trade.
            # It decides for itself whether this archive qualifies (completed,
            # order-less, and not already standing per part).
            #
            # ⚠️ The wrapper runs that credit inside a SAVEPOINT of its own,
            # and this call site is why. Swallowing the exception is only half
            # of "never fails its caller": a failure that reached the DATABASE
            # rolls the session's transaction back with it, and by the time the
            # polite empty list came back, the file copy and every field written
            # above would be gone — lost to bookkeeping that is explicitly
            # optional. The savepoint keeps the loss inside the ledger; the
            # ``commit`` below still writes the attach.
            if archive.status == "completed" and archive.project_id is None:
                from backend.app.services import part_stock

                await part_stock.credit_if_unfiled(self.db, archive)

            await self.db.commit()
            committed = True
            try:
                await self.db.refresh(archive)
            except Exception:
                # The durable attach has succeeded.  A later refresh may fail
                # because a concurrent reader or connection went away; it must
                # not be reported as a failed recovery or remove its file.
                logger.exception("attach_3mf_to_archive committed but refresh failed for archive %s", archive_id)
            return True
        except _AttachPreparationStale:
            raise
        except Exception as e:
            logger.exception("attach_3mf_to_archive failed for archive %s: %s", archive_id, e)
            if not committed:
                await self.db.rollback()
                if created_archive_dir is not None:
                    durable_reference = await _attach_output_is_durably_referenced(
                        self.db,
                        archive_id,
                        published_file_path,
                    )
                    if durable_reference is False:
                        try:
                            # This directory was created by this invocation only;
                            # never use a guessed stem or a reused donor path here.
                            shutil.rmtree(created_archive_dir)
                        except OSError:
                            logger.warning(
                                "attach_3mf_to_archive could not clean its staging directory for archive %s",
                                archive_id,
                            )
                    else:
                        logger.error(
                            "attach_3mf_to_archive preserved output for archive %s after ambiguous commit (referenced=%s)",
                            archive_id,
                            durable_reference,
                        )
            return False
        finally:
            if file_reference_scope is not None:
                await file_reference_scope.__aexit__(None, None, None)

    async def mark_3mf_unavailable(self, archive_id: int, reason: str | None = None) -> bool:
        """Record a real 3MF recovery failure without overwriting a success.

        An empty ``file_path`` while FTP is still in flight is not a failure.
        Call this only after a download/attach attempt ended unsuccessfully.

        *reason* is ``archive_download.last_download_failure_reason`` — what the
        Archives banner words itself by (audit D6). A failure with no known
        reason (the file came down but would not attach) drops an earlier one
        rather than leave it describing an attempt it no longer matches.
        """
        from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

        try:
            async with archive_write_scope(self.db, archive_id):
                archive = await load_active_archive_for_write(self.db, archive_id)
                if archive is None or archive.file_path:
                    await self.db.rollback()
                    return False
                extra = {**(archive.extra_data or {}), "no_3mf_available": True}
                if reason:
                    extra["no_3mf_reason"] = reason
                else:
                    extra.pop("no_3mf_reason", None)
                archive.extra_data = extra
                await self.db.commit()
                return True
        except Exception:
            await self.db.rollback()
            logger.exception("Could not record 3MF recovery failure for archive %s", archive_id)
            return False

    async def get_archive(self, archive_id: int, *, include_trashed: bool = False) -> PrintArchive | None:
        """Get an archive by ID with relationships loaded.

        Trashed archives return None unless ``include_trashed=True`` so user-
        facing GET /archives/{id} 404s on trashed rows. Internal callers (e.g.
        the trash routes themselves, restore flows) pass the flag explicitly.
        """
        from sqlalchemy.orm import selectinload

        conditions = [PrintArchive.id == archive_id]
        if not include_trashed:
            conditions.append(PrintArchive.deleted_at.is_(None))
        result = await self.db.execute(
            select(PrintArchive)
            .options(selectinload(PrintArchive.created_by), selectinload(PrintArchive.project))
            .where(*conditions)
        )
        return result.scalar_one_or_none()

    async def update_archive_status(
        self,
        archive_id: int,
        status: str,
        completed_at: datetime | None = None,
        failure_reason: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update the status of an archive.

        ``failure_reason`` is the short cause code (VARCHAR(100), e.g.
        "Filament runout"); ``error_message`` is the verbose diagnostic text
        (TEXT) carried over from the queue item on failure.
        """
        archive = await self.get_archive(archive_id)
        if not archive:
            return False

        archive.status = status
        if completed_at:
            archive.completed_at = completed_at
        if failure_reason:
            archive.failure_reason = failure_reason
        if error_message and not archive.error_message:
            archive.error_message = error_message

        await self.db.commit()
        return True

    async def list_archives(
        self,
        printer_id: int | None = None,
        project_id: int | None = None,
        library_file_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        search: str | None = None,
        collection: str | None = None,
        material: str | None = None,
        colors: list[str] | None = None,
        color_mode: str = "or",
        favorites_only: bool = False,
        hide_failed: bool = False,
        hide_duplicates: bool = False,
        tag: str | None = None,
        kind: str | None = None,
        sort_by: str = "date-desc",
        limit: int | None = 50,
        offset: int = 0,
        visible_to_user_id: int | None = None,
    ) -> tuple[list[PrintArchive], int]:
        """List archives with server-side filtering, sorting and pagination.

        Returns (items, total_count).

        ``visible_to_user_id`` scopes the listing to a single owner — passed by
        the route when the caller holds ``archives:read_own`` but not
        ``archives:read_all`` (ownership read-split, security #2). None = no
        ownership scoping (caller can read all).
        """
        from sqlalchemy.orm import selectinload

        # Trashed archives never appear in the main listing — they live in
        # the archive trash bin until restored or hard-deleted by the sweeper.
        filters = [PrintArchive.deleted_at.is_(None)]

        # Ownership scoping (security #2) — only the caller's own runs.
        if visible_to_user_id is not None:
            filters.append(PrintArchive.created_by_id == visible_to_user_id)

        # Printer / project filters
        if printer_id:
            filters.append(PrintArchive.printer_id == printer_id)
        if project_id:
            filters.append(PrintArchive.project_id == project_id)
        # Library-file filter — every print dispatched from one library file.
        # background_dispatch stamps PrintArchive.library_file_id at dispatch
        # time; external/manual prints have it NULL and never match.
        if library_file_id:
            filters.append(PrintArchive.library_file_id == library_file_id)

        # Date range
        if date_from:
            dt_from = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
            filters.append(PrintArchive.created_at >= dt_from)
        if date_to:
            dt_to = datetime.combine(date_to, time.max, tzinfo=timezone.utc)
            filters.append(PrintArchive.created_at <= dt_to)

        # Search (LIKE on print_name and filename)
        if search and search.strip():
            search_term = f"%{search.strip()}%"
            filters.append(
                or_(
                    PrintArchive.print_name.ilike(search_term),
                    PrintArchive.filename.ilike(search_term),
                )
            )

        # Collection presets
        if collection:
            now = datetime.now(timezone.utc)
            if collection == "recent":
                filters.append(PrintArchive.created_at >= now - timedelta(hours=24))
            elif collection == "this-week":
                filters.append(PrintArchive.created_at >= now - timedelta(days=7))
            elif collection == "this-month":
                first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                filters.append(PrintArchive.created_at >= first_of_month)
            elif collection == "favorites":
                filters.append(PrintArchive.is_favorite == True)  # noqa: E712
            elif collection == "printed":
                # Any final-status archive — covers a print attempt regardless
                # of outcome. The narrower Failed collection covers the
                # failure subset.
                filters.append(PrintArchive.status.in_(["completed", "failed", "aborted", "cancelled", "stopped"]))
            elif collection == "failed":
                filters.append(PrintArchive.status.in_(["failed", "aborted", "cancelled"]))
            elif collection == "duplicates":
                # Source files that appear more than once, keyed on the same
                # effective_hash = COALESCE(source_content_hash, content_hash) —
                # with the same trashed/NULL guards — as the duplicate badge
                # (get_duplicate_hashes_and_names) and hide_duplicates, so all
                # three agree. content_hash alone missed reprints whose applied
                # patches differ (same source file, different FTP'd bytes).
                eff_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
                dup_hashes = (
                    select(eff_hash)
                    .where(PrintArchive.content_hash.isnot(None), PrintArchive.deleted_at.is_(None))
                    .group_by(eff_hash)
                    .having(func.count(PrintArchive.id) > 1)
                    .scalar_subquery()
                )
                filters.append(eff_hash.in_(dup_hashes))

        # Material filter (comma-separated field)
        if material:
            filters.append(PrintArchive.filament_type.ilike(f"%{material}%"))

        # Color filter (comma-separated field, OR/AND mode)
        if colors:
            color_conditions = [PrintArchive.filament_color.ilike(f"%{c}%") for c in colors]
            if color_mode == "and":
                filters.extend(color_conditions)  # All must match
            else:
                filters.append(or_(*color_conditions))  # Any must match

        # Favorites toggle
        if favorites_only:
            filters.append(PrintArchive.is_favorite == True)  # noqa: E712

        # Hide failed (inverse of the Failed collection — keep the status sets in sync)
        if hide_failed:
            filters.append(PrintArchive.status.notin_(["failed", "aborted", "cancelled"]))

        # Tag filter (comma-separated field)
        if tag:
            filters.append(PrintArchive.tags.ilike(f"%{tag}%"))

        # Calibration kind filter. Archive is print-history-only (m062+), so
        # the old "gcode vs source-only" split lost meaning — every row has a
        # print attached. Now we split on the calibration flag the wizard sets.
        if kind == "calibration":
            filters.append(PrintArchive.is_calibration.is_(True))
        elif kind == "regular":
            filters.append(PrintArchive.is_calibration.is_(False))

        # Hide duplicates: collapse reprints of the same *source* file to one row.
        if hide_duplicates:
            # A duplicate is a reprint of the same source 3MF, so we key on the
            # chain-root hash effective_hash = COALESCE(source_content_hash,
            # content_hash) — the SAME key the duplicate badge groups on
            # (get_duplicate_hashes_and_names). content_hash is the *patched,
            # FTP'd* bytes and differs per applied-patch outcome (e.g.
            # mesh_mode_fast_check patched on one printer but not another), so
            # keying on it left genuine reprints un-collapsed — that was the bug.
            #
            # The "first per hash" subquery must apply the SAME filters as the
            # outer query (printer, collection, date, non-trashed, …). Keying it
            # globally lets the kept min-id row sit OUTSIDE an active filter — a
            # copy on another printer, or a trashed copy — so `id IN (…)` then
            # matches nothing in the filtered view and every visible copy is
            # wrongly hidden (e.g. printer_id=1 returns zero rows though printer 1
            # has the file).
            #
            # effective_hash can legitimately be NULL — fallback archives created
            # before the 3MF is downloaded, and re-sliced archives, carry neither
            # hash — so those rows are never collapsed (always kept).
            eff_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
            first_per_hash = (
                select(func.min(PrintArchive.id))
                .select_from(PrintArchive)
                .where(*filters, eff_hash.isnot(None))
                .group_by(eff_hash)
                .scalar_subquery()
            )
            filters.append(
                or_(
                    eff_hash.is_(None),
                    PrintArchive.id.in_(first_per_hash),
                )
            )

        # Sorting
        order_clause = PrintArchive.created_at.desc()  # default
        if sort_by == "date-asc":
            order_clause = PrintArchive.created_at.asc()
        elif sort_by == "name-asc":
            order_clause = func.coalesce(PrintArchive.print_name, PrintArchive.filename).asc()
        elif sort_by == "name-desc":
            order_clause = func.coalesce(PrintArchive.print_name, PrintArchive.filename).desc()
        elif sort_by == "size-desc":
            order_clause = PrintArchive.file_size.desc()
        elif sort_by == "size-asc":
            order_clause = PrintArchive.file_size.asc()
        elif sort_by in _MEASURED_SORTS:
            # What the print actually cost: money, energy, filament, time.
            #
            # ⚠️ **Empty values are held LAST in BOTH directions.** These columns
            # are routinely NULL — an external print has no cost, a printer with
            # no smart plug has no energy, a running print has no duration — and
            # the two backends disagree about where NULL sorts: PostgreSQL puts
            # it high, SQLite low. Left alone, the same click would open on a
            # screenful of blanks on one backend and on data on the other.
            # ``is_(None)`` as the leading key expresses that in plain SQL,
            # which both dialects order identically (False < True).
            column, direction = _MEASURED_SORTS[sort_by]
            order_clause = column.is_(None), (column.asc() if direction == "asc" else column.desc())
        elif sort_by in ("printer-asc", "printer-desc"):
            # A correlated subquery rather than a join: an archive can have no
            # printer at all (an external print, or one whose printer was
            # deleted), and an inner join would drop those rows from a list that
            # is only being re-ordered. COALESCE rather than a NULLS clause,
            # whose support differs between our two backends — and it falls back
            # to the same value the row DISPLAYS (`sliced_for_model`), so the
            # order matches the column on screen instead of an id nobody sees.
            printer_name = select(Printer.name).where(Printer.id == PrintArchive.printer_id).scalar_subquery()
            printer_sort = func.coalesce(printer_name, PrintArchive.sliced_for_model, "")
            order_clause = printer_sort.asc() if sort_by == "printer-asc" else printer_sort.desc()

        # ⚠️ **A stable tiebreak, and it is load-bearing because this list
        # PAGES.** Rows sharing a sort key have no defined order between two
        # queries, so paging a low-cardinality sort — by printer across a farm
        # of twelve, or by a cost every failed print left NULL — could repeat
        # one archive on page 2 and skip another entirely. ``id`` is unique and
        # never NULL, so it always orders the ties.
        order_clauses = list(order_clause) if isinstance(order_clause, tuple) else [order_clause]
        order_clauses.append(PrintArchive.id.desc())

        # Total count (same filters, no limit/offset)
        count_query = select(func.count()).select_from(PrintArchive).where(*filters)
        total = (await self.db.execute(count_query)).scalar() or 0

        # Data query — limit=None means "no pagination, return all matching rows".
        query = (
            select(PrintArchive)
            .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
            .where(*filters)
            .order_by(*order_clauses)
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        result = await self.db.execute(query)
        items = list(result.scalars().all())

        return items, total

    async def get_filter_options(self) -> dict:
        """Get distinct filter values for archive dropdowns."""
        # Materials
        mat_result = await self.db.execute(
            select(PrintArchive.filament_type)
            .where(PrintArchive.filament_type.isnot(None), PrintArchive.filament_type != "")
            .distinct()
        )
        raw_materials = [r[0] for r in mat_result.all()]
        # Flatten comma-separated values
        materials = sorted({m.strip() for raw in raw_materials for m in raw.split(",") if m.strip()})

        # Colors
        col_result = await self.db.execute(
            select(PrintArchive.filament_color)
            .where(PrintArchive.filament_color.isnot(None), PrintArchive.filament_color != "")
            .distinct()
        )
        raw_colors = [r[0] for r in col_result.all()]
        colors = sorted({c.strip() for raw in raw_colors for c in raw.split(",") if c.strip()})

        # Tags
        tag_result = await self.db.execute(
            select(PrintArchive.tags).where(PrintArchive.tags.isnot(None), PrintArchive.tags != "").distinct()
        )
        raw_tags = [r[0] for r in tag_result.all()]
        tags = sorted({t.strip() for raw in raw_tags for t in raw.split(",") if t.strip()})

        return {"materials": materials, "colors": colors, "tags": tags}

    async def delete_archive(self, archive_id: int) -> bool:
        """Hard-delete one archive under the shared archive-facts writer guard."""
        from backend.app.services.archive_write_scope import archive_write_scope

        async with archive_write_scope(self.db, archive_id):
            deleted = await self._delete_archive_locked(archive_id)
        if deleted:
            _remove_id_owned_folders(archive_id)
        return deleted

    async def _delete_archive_locked(self, archive_id: int) -> bool:
        """Hard-delete an archive: its row, and its files on disk.

        ⚠️ **``include_trashed=True`` is the whole of this function working at
        all.** Every trash-side caller hands it an archive that is *already*
        soft-deleted — ``hard_delete_now`` says so in its own docstring — and
        ``get_archive`` drops rows with ``deleted_at`` by default. So from
        ``fb448f9d`` (v0.4.2) until this was noticed, it answered ``False`` and
        did nothing: the retention sweeper removed rows through its own fallback
        statement and leaked every file, and "Empty trash" removed nothing at
        all. A hard delete is by definition about a row on its way out; refusing
        the ones already on their way out was backwards.
        """
        archive = await self.get_archive(archive_id, include_trashed=True)
        if not archive:
            return False

        # Archive rows are navigation/provenance after a queue item has a
        # ready queue source of its own.  Hard deletion therefore only cancels
        # legacy dependents and explicitly clears both queue tiers; SQLite does
        # not enforce the model's ON DELETE SET NULL rule.
        from backend.app.services import queue_source_release

        await queue_source_release.source_purged(
            self.db,
            archive_ids=[archive_id],
            reason=queue_source_release.REASON_ARCHIVE_DELETED,
        )

        # Detach spool-usage history before removing the archive. The
        # ``spool_usage_history.archive_id`` FK has no ``ON DELETE`` clause (the
        # row must outlive the archive so the spool keeps its consumption record),
        # so NULL it here: on SQLite this avoids a dangling reference, on
        # PostgreSQL it avoids a foreign-key violation that would block the delete.
        # Done before every ``db.delete(archive)`` path below (incl. the early
        # security returns) so the subsequent commit flushes both together.
        from backend.app.models.spool_usage_history import SpoolUsageHistory

        await self.db.execute(
            update(SpoolUsageHistory).where(SpoolUsageHistory.archive_id == archive_id).values(archive_id=None)
        )

        # Usage-journal rows die with their archive. Explicit because the FK
        # CASCADE fires on PostgreSQL only; placed beside the history detach so
        # every ``db.delete(archive)`` path below flushes both together.
        from backend.app.services.print_usage_journal import delete_for_archive

        await delete_for_archive(self.db, archive_id)

        # Completion receipts are disposable run-bound acknowledgements.  The
        # model declares CASCADE, but SQLite does not enforce it in every
        # deployment, so remove them explicitly with the hard delete.
        from sqlalchemy import delete

        from backend.app.models.print_completion_receipt import PrintCompletionReceipt

        await self.db.execute(delete(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id))

        # Free-stock movements go the OTHER way: the parts this print made are
        # on a shelf and stay there (pass 8, §Invariants touched — "deleting an
        # archive does not delete its movements"), so the link is cut and the
        # rows are kept. ``ON DELETE SET NULL`` says the same thing and fires on
        # PostgreSQL only; on SQLite the movements would keep an id naming
        # nothing, which is both a dead link in the product's history and an
        # idempotency key the next archive to take that rowid would trip over.
        from backend.app.services.part_stock import detach_archive

        await detach_archive(self.db, archive_id)

        # Resolve the directory to delete BEFORE committing the DB change
        dir_to_delete: Path | None = None

        if archive.file_path and archive.file_path.strip():
            file_path = settings.base_dir / archive.file_path
            if file_path.exists():
                archive_dir = file_path.parent

                # Safety check 1: archive_dir must be inside archive_dir
                try:
                    archive_dir.resolve().relative_to(settings.archive_dir.resolve())
                except ValueError:
                    logger.error(
                        f"SECURITY: Refusing to delete archive {archive_id} - "
                        f"path {archive_dir} is outside archive directory {settings.archive_dir}"
                    )
                    await self.db.delete(archive)
                    await self.db.commit()
                    return True

                # Safety check 2: archive_dir must be at least 2 levels deep. Every
                # archive folder is (``<printer>/<dated folder>/``, ``no_source/<id>/``);
                # one level up is a PRINTER's folder, holding every print it made, and
                # a file_path that lost a component points there (upstream #2968 —
                # the guard used to be ``< 1``, which let that through).
                try:
                    relative_path = archive_dir.resolve().relative_to(settings.archive_dir.resolve())
                    if len(relative_path.parts) < 2:
                        logger.error(
                            f"SECURITY: Refusing to delete archive {archive_id} - "
                            f"path {archive_dir} is not deep enough inside archive directory"
                        )
                        await self.db.delete(archive)
                        await self.db.commit()
                        return True
                except ValueError:
                    pass  # Already handled above

                dir_to_delete = archive_dir
        # An empty file_path is a normal archive — created at print start, or a
        # job that never yields a 3MF — not an attack: the folders it owns by id
        # go through ``_remove_id_owned_folders`` once the row is gone. It used
        # to log an ERROR under a SECURITY banner on every such delete
        # (upstream #2968).

        from backend.app.services.archive_write_scope import archive_file_reference_scope

        async def _delete_with_fresh_file_refcount() -> bool:
            # This is the same scope an attach enters after it finds a donor.
            # It closes the gap where delete counted one donor, attach adopted
            # its path, then delete removed the sole physical directory.
            shared_result = await self.db.execute(
                select(func.count(PrintArchive.id)).where(
                    PrintArchive.file_path == archive.file_path,
                    PrintArchive.id != archive_id,
                )
            )
            shared = (shared_result.scalar() or 0) > 0
            await self.db.delete(archive)
            await self.db.commit()
            return shared

        if archive.file_path:
            async with archive_file_reference_scope(self.db, archive.file_path):
                shared = await _delete_with_fresh_file_refcount()
        else:
            # No bytes are named, hence no attach donor can share this row.
            await self.db.delete(archive)
            await self.db.commit()
            shared = False

        # Only delete files AFTER the DB commit succeeds and no other archives reference them
        if dir_to_delete and not shared:
            shutil.rmtree(dir_to_delete, ignore_errors=True)

        return True

    async def attach_timelapse(
        self,
        archive_id: int,
        timelapse_data: bytes,
        filename: str = "timelapse.mp4",
    ) -> bool:
        """Attach a timelapse video to an archive.

        Non-MP4 videos (e.g. AVI from P1S) are saved as-is and a background
        task converts them to MP4 for browser compatibility.
        """
        import asyncio

        archive = await self.get_archive(archive_id)
        if not archive:
            return False

        # Where this archive's files live — the shared helper, never derived
        # from ``file_path`` by hand. A print whose 3MF could not be fetched
        # (``no_3mf_reason`` says why) has ``file_path == ""``, and ``(base_dir / "").parent``
        # is the PARENT of the data directory: in Docker that is /app, so the
        # write failed with EACCES and the video was fetched again and
        # discarded over and over; where the parent was writable it landed
        # beside the install and the attach failed anyway on relative_to
        # below (upstream #2843).
        from backend.app.utils.archive_paths import archive_dir_for

        archive_dir = archive_dir_for(archive)

        # ``filename`` originates from a printer's FTP directory listing (the
        # printer is part of the trust surface — a compromised/malicious printer
        # can return names with ``..`` segments) or the ?filename= query param.
        # Contain the write to archive_dir; on escape, refuse rather than raise
        # 400 from this background-task context (path-traversal hardening,
        # GHSA-r2qv).
        try:
            timelapse_file = safe_join_under(archive_dir, filename, http=False)
        except PathTraversalError:
            logger.warning("attach_timelapse: rejected traversal filename %r for archive %s", filename, archive_id)
            return False

        # Created only once the name has been vetted, so a rejected filename
        # leaves nothing behind. A no-3MF archive has never had a folder of its
        # own, and the timelapse can be the first thing to want one.
        await asyncio.to_thread(lambda: timelapse_file.parent.mkdir(parents=True, exist_ok=True))
        # Save timelapse - use thread pool to avoid blocking event loop
        # (timelapse files can be 100MB+, sync write blocks for seconds)
        await asyncio.to_thread(timelapse_file.write_bytes, timelapse_data)

        # Update archive record
        archive.timelapse_path = str(timelapse_file.relative_to(settings.base_dir))
        await self.db.commit()

        # For non-MP4 videos (e.g. AVI from P1S), kick off background conversion
        if not filename.lower().endswith(".mp4"):
            spawn_background_task(
                _convert_timelapse_to_mp4(archive_id, timelapse_file),
                name=f"timelapse-convert-{archive_id}",
            )

        return True


def _remove_id_owned_folders(archive_id: int) -> None:
    """Remove the folders an archive owns by its id, once its row is gone.

    Whatever its ``file_path`` says now: the fallback it used while it had no
    3MF (``no_source/<id>/`` — photos, timelapse, design file, source 3MF) and
    the photo folder of the fallback before that (``<id>/photos/``). Both are
    this archive's alone, never shared by content-hash dedup the way a 3MF
    folder is. Before, nothing removed them — the old fallback sat in printer
    folders and could not be deleted safely — so every such archive left its
    files behind.

    ⚠️ Never ``archive/<id>/`` itself: it may be printer <id>'s folder, full of
    other archives (utils/archive_paths).
    """
    from backend.app.utils.archive_paths import fallback_dir_for, legacy_photos_dir_for

    for folder in (fallback_dir_for(archive_id), legacy_photos_dir_for(archive_id)):
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


async def _convert_timelapse_to_mp4(archive_id: int, source_path: Path) -> None:
    """Background task: convert non-MP4 timelapse (e.g. AVI from P1S) to MP4.

    Runs with low CPU priority (-threads 1, nice) so it doesn't starve
    other processes on resource-constrained devices like Raspberry Pi.
    """
    import asyncio

    from backend.app.core.database import async_session
    from backend.app.services.camera import get_ffmpeg_path

    logger = logging.getLogger(__name__)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        logger.info(
            "FFmpeg not available, skipping timelapse conversion for archive %s (file saved as %s)",
            archive_id,
            source_path.suffix,
        )
        return

    mp4_path = source_path.with_suffix(".mp4")

    try:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-threads",
            "1",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ]

        # Try with nice for lower CPU priority (standard on Linux/macOS)
        try:
            process = await asyncio.create_subprocess_exec(
                "nice",
                "-n",
                "19",
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # nice not available (e.g. Windows), run without
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            logger.warning(
                "Timelapse conversion failed for archive %s: %s",
                archive_id,
                summarize_ffmpeg_stderr(stderr) or NO_FFMPEG_OUTPUT,
            )
            if mp4_path.exists():
                mp4_path.unlink()
            return

        # Update DB path to the new MP4 file
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                archive.timelapse_path = str(mp4_path.relative_to(settings.base_dir))
                await db.commit()

        # Remove original non-MP4 file
        if source_path.exists():
            source_path.unlink()

        logger.info(
            "Converted timelapse to MP4 for archive %s (%s → %s)",
            archive_id,
            source_path.name,
            mp4_path.name,
        )

    except Exception as e:
        logger.warning("Timelapse conversion error for archive %s: %s", archive_id, e)
        if mp4_path.exists():
            mp4_path.unlink()
