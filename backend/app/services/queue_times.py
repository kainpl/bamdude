"""A queue row's print time — one reader shared by the queue route and the
farm forecast (#2573, farm-forecast task 1).

Moved out of ``api/routes/print_queue.py``, which used to keep this cache to
itself even though the forecast loader (task 3) needs the exact same "what is
this row's estimated print time" answer. Behaviour is unchanged: archive-first
(row estimate, then the plate's if the 3MF is on disk), else the library
file's metadata + plate.
"""

import logging
import zipfile
from collections import OrderedDict
from pathlib import Path
from threading import Lock

import defusedxml.ElementTree as ET

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services.product_composition import plate_filaments
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor
from backend.app.utils.threemf_tools import extract_bed_type_from_3mf, extract_filament_usage_from_3mf

logger = logging.getLogger(__name__)


def extract_print_time_from_3mf(file_path: Path, plate_id: int | None = None) -> int | None:
    """Extract print time (prediction) from a 3MF file.

    Args:
        file_path: Path to the 3MF file
        plate_id: Optional plate index to filter for (for multi-plate files)

    Returns:
        Print time in seconds, or None if not found
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            if plate_id is not None:
                for plate_elem in root.findall(".//plate"):
                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                plate_index = int(meta.get("value", "0"))
                            except ValueError:
                                pass  # Skip plate with unparseable index
                            break

                    if plate_index == plate_id:
                        for meta in plate_elem.findall("metadata"):
                            if meta.get("key") == "prediction":
                                try:
                                    return int(meta.get("value", "0"))
                                except ValueError:
                                    return None
                        break
            else:
                plate_elem = root.find(".//plate")
                if plate_elem is not None:
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "prediction":
                            try:
                                return int(meta.get("value", "0"))
                            except ValueError:
                                return None
    except Exception as e:
        logger.warning("Failed to extract print time from %s: %s", file_path, e)

    return None


# Per-plate 3MF metadata cache for queue listing (#2573). A queue poll enriches
# every row, and each row previously opened + parsed its 3MF THREE times (print
# time, filament usage, bed type). On a farm with a busy queue, several browsers
# polling every few seconds re-parsed the same unchanged files constantly. Cache
# the combined (print_time, filament_grams, bed_type) tuple keyed by file
# revision — an unchanged file is parsed at most once; a replaced/edited file
# (different mtime/size) re-parses automatically. LRU-bounded so it can't grow
# without limit. Locked because FastAPI runs handlers across a thread pool.
_PLATE_META_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_PLATE_META_LOCK = Lock()
_PLATE_META_MAX = 512


def plate_metadata_cached(file_path: Path, plate_id: int | None) -> tuple[int | None, float, str | None]:
    """Return (print_time_seconds, filament_grams, bed_type) for a plate, cached
    by file revision so queue polling parses each 3MF once instead of 3x (#2573)."""
    try:
        st = file_path.stat()
    except OSError:
        return None, 0.0, None
    key = (str(file_path), plate_id, st.st_mtime_ns, st.st_size)
    with _PLATE_META_LOCK:
        hit = _PLATE_META_CACHE.get(key)
        if hit is not None:
            _PLATE_META_CACHE.move_to_end(key)
            return hit

    print_time = extract_print_time_from_3mf(file_path, plate_id)
    filament_grams = sum(f["used_g"] for f in extract_filament_usage_from_3mf(file_path, plate_id))
    bed_type = extract_bed_type_from_3mf(file_path, plate_id)
    result: tuple[int | None, float, str | None] = (print_time, filament_grams, bed_type)

    with _PLATE_META_LOCK:
        _PLATE_META_CACHE[key] = result
        _PLATE_META_CACHE.move_to_end(key)
        while len(_PLATE_META_CACHE) > _PLATE_META_MAX:
            _PLATE_META_CACHE.popitem(last=False)
    return result


def plate_metadata_for_row(
    *, plate_id: int | None, descriptor: QueueSourceDescriptor | None
) -> tuple[int | None, float, str | None]:
    """``(print_time, filament_grams, bed_type)`` of a job's own captured bytes (m173).

    The snapshot is the source of truth for what a queued job IS (spec §4, A09):
    its original row may be gone, and while it exists it may have been re-sliced
    since — A03 says the job keeps the bytes it accepted, so the card has to
    describe those. One call, three values, because the three parsers share one
    cache entry.

    **Where the answer is cached:** :func:`plate_metadata_cached`, unchanged — the
    same module-level LRU the original path has always used, keyed by the file's
    own revision. For a content-addressed immutable object that key can never go
    stale, so a queue poll opens the ZIP once per (object, plate) and every later
    poll of every row is a dict lookup.

    ``(None, 0.0, None)`` for a job with no snapshot, and for one whose object is
    missing or unreadable — never a fall back to another file (S7).
    """
    if descriptor is None:
        return None, 0.0, None
    return plate_metadata_cached(descriptor.path, plate_id or descriptor.plate_fallback)


def print_time_for_row(
    *,
    archive: PrintArchive | None,
    library_file: LibraryFile | None,
    plate_id: int | None,
    descriptor: QueueSourceDescriptor | None = None,
) -> int | None:
    """A queue row's print time, the way the queue response derives it.

    The job's own captured bytes first when it has them (m173). Then archive (its
    own estimate, then the plate's if the 3MF is on disk), else the library file
    (its metadata, then the plate's). ``None`` = the row has no estimate — callers
    must not invent one. ``is_file()``, never ``exists()``: a blank ``file_path``
    resolves to the data directory.

    A snapshot whose plate carries no ``prediction`` falls through to the row's
    own recorded estimate, which is not a guess: it was read out of the same bytes
    when the row was written. A job with neither answers ``None``.
    """
    snapshot_time, _grams, _bed = plate_metadata_for_row(plate_id=plate_id, descriptor=descriptor)
    if snapshot_time is not None:
        return snapshot_time
    if archive is not None and archive.deleted_at is None:
        seconds = archive.print_time_seconds
        if plate_id:
            path = settings.base_dir / archive.file_path
            if path.is_file():
                plate_time, _grams, _bed = plate_metadata_cached(path, plate_id)
                if plate_time is not None:
                    seconds = plate_time
        return seconds
    if library_file is not None:
        meta = library_file.file_metadata or {}
        seconds = meta.get("print_time_seconds")
        if plate_id:
            raw = Path(library_file.file_path)
            path = raw if raw.is_absolute() else settings.base_dir / library_file.file_path
            if path.is_file():
                plate_time, _grams, _bed = plate_metadata_cached(path, plate_id)
                if plate_time is not None:
                    seconds = plate_time
        return int(seconds) if isinstance(seconds, (int, float)) else None
    return None


def filaments_for_row(
    *,
    archive: PrintArchive | None,
    library_file: LibraryFile | None,
    plate_id: int | None,
    descriptor: QueueSourceDescriptor | None = None,
) -> list[dict] | None:
    """The slicer's filaments (type, colour, ``used_g``) of a queue row's plate.

    A job with a captured source reads that (m173) — the bytes it will actually
    print, whatever became of the row it came from. Library rows read the metadata
    already on the row — plate ``plate_id``, or the whole file when the row names
    none. Archive rows read the 3MF on disk; an archive without its file answers
    ``None``, never a guess.
    """
    if descriptor is not None:
        if not descriptor.path.is_file():
            return None
        return extract_filament_usage_from_3mf(descriptor.path, plate_id or descriptor.plate_fallback) or None
    if archive is not None and archive.deleted_at is None:
        path = settings.base_dir / archive.file_path
        if archive.file_path and path.is_file():
            return extract_filament_usage_from_3mf(path, plate_id) or None
        return None
    if library_file is not None:
        filaments = plate_filaments(library_file.file_metadata, plate_id or 0)
        return filaments or None
    return None
