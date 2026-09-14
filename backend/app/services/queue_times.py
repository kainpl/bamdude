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
from stat import S_ISREG
from threading import Lock
from typing import NamedTuple

import defusedxml.ElementTree as ET

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services.product_composition import plate_filaments
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor
from backend.app.utils.threemf_tools import (
    extract_bed_type_from_3mf,
    extract_filament_usage_from_3mf,
    plate_picture_entry,
)

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
# the combined facts keyed by file revision — an unchanged file is parsed at most
# once; a replaced/edited file (different mtime/size) re-parses automatically.
# LRU-bounded so it can't grow without limit. Locked because FastAPI runs handlers
# across a thread pool.
#
# ⚠️ The entry carries the per-slot filament LIST as well as the summed weight, and
# that is not redundancy: the weight is `sum(f["used_g"] …)` of exactly that list,
# so the order page's "what does this order still need" reader (which wants the
# list) and the queue card (which wants the sum) are one parse, not two. It used to
# be two — and the list side had no cache at all, so a quantity-20 line opened the
# same ZIP twenty times per request.
_PLATE_META_CACHE: "OrderedDict[tuple, _PlateFacts]" = OrderedDict()
_PLATE_META_LOCK = Lock()
_PLATE_META_MAX = 512


class _PlateFacts(NamedTuple):
    """Everything one plate of one file revision can be asked, read in one pass."""

    print_time: int | None
    filament_grams: float
    bed_type: str | None
    filaments: tuple[dict, ...]


_NO_FACTS = _PlateFacts(None, 0.0, None, ())


def _plate_facts(file_path: Path, plate_id: int | None) -> _PlateFacts:
    """The cached facts for one plate of one file revision — the only parser here.

    ``_NO_FACTS`` when there is no regular file there, which is how a missing
    snapshot object answers nothing rather than raising — and ⚠️ **a regular file,
    not merely something that stats**: an archive created at print start carries
    ``file_path=""`` until its 3MF arrives, and an empty path resolves to the data
    DIRECTORY, which `stat()` happily describes. The three parsers below then each
    opened the data directory as a ZIP and logged "Is a directory" on every poll
    (#2573). The callers' own ``is_file()`` guards stay; this closes the case for
    the ones that do not have one.
    """
    try:
        st = file_path.stat()
    except OSError:
        return _NO_FACTS
    if not S_ISREG(st.st_mode):
        return _NO_FACTS
    key = (str(file_path), plate_id, st.st_mtime_ns, st.st_size)
    with _PLATE_META_LOCK:
        hit = _PLATE_META_CACHE.get(key)
        if hit is not None:
            _PLATE_META_CACHE.move_to_end(key)
            return hit

    filaments = tuple(extract_filament_usage_from_3mf(file_path, plate_id) or ())
    facts = _PlateFacts(
        print_time=extract_print_time_from_3mf(file_path, plate_id),
        filament_grams=sum(f["used_g"] for f in filaments),
        bed_type=extract_bed_type_from_3mf(file_path, plate_id),
        filaments=filaments,
    )

    with _PLATE_META_LOCK:
        _PLATE_META_CACHE[key] = facts
        _PLATE_META_CACHE.move_to_end(key)
        while len(_PLATE_META_CACHE) > _PLATE_META_MAX:
            _PLATE_META_CACHE.popitem(last=False)
    return facts


def plate_metadata_cached(file_path: Path, plate_id: int | None) -> tuple[int | None, float, str | None]:
    """Return (print_time_seconds, filament_grams, bed_type) for a plate, cached
    by file revision so queue polling parses each 3MF once instead of 3x (#2573)."""
    facts = _plate_facts(file_path, plate_id)
    return facts.print_time, facts.filament_grams, facts.bed_type


def plate_filaments_cached(file_path: Path, plate_id: int | None) -> list[dict] | None:
    """The per-slot filaments of one plate, out of the same entry as the weight.

    ``None`` — never ``[]`` — when the file said nothing, because every caller
    reads "no readable plate" off that (``filament_needs.QueuedNeed.filaments``
    is documented as ``None = the row has no readable plate``, and an empty list
    would be counted as "needs nothing").
    """
    return [dict(f) for f in _plate_facts(file_path, plate_id).filaments] or None


# The same discipline for the row's PICTURE (m173, spec §4 / A09). A queue row
# has to say whether it HAS one before anything asks for it — otherwise the UI is
# guessing, and a list of fifty rows must not become fifty archive reads. So the
# question "which entry is this plate's render" is answered from the container's
# central directory (a namelist read, no decompression) and cached under the same
# key shape as the metadata above: an unchanged file is asked once.
#
# ⚠️ Separate from ``_PLATE_META_CACHE`` on purpose rather than a fourth value in
# its tuple: that tuple is returned by a public helper with three callers outside
# this module, and widening it would reach all of them for a value none of them
# wants. Both caches carry the same key and the same LRU bound.
#
# ⚠️ **Nothing has to invalidate either one.** The key is the file's own revision
# (path + mtime + size), so a replaced file re-reads itself — and a snapshot's
# path is content-addressed and immutable (S3), which makes the entry provably
# still valid for exactly the files this reader is for.
_PLATE_PICTURE_CACHE: "OrderedDict[tuple, str | None]" = OrderedDict()
_PLATE_PICTURE_LOCK = Lock()
_PLATE_PICTURE_MAX = 512


def plate_picture_cached(file_path: Path, plate_id: int | None) -> str | None:
    """The ZIP entry of a plate's render inside ``file_path``, cached by revision.

    ``None`` for a file that is missing, is not a container, or simply does not
    render that plate — a missing picture is not an error (spec §4: the UI shows
    its honest empty state), so nothing here raises and nothing logs per poll.
    """
    try:
        st = file_path.stat()
    except OSError:
        return None
    key = (str(file_path), plate_id, st.st_mtime_ns, st.st_size)
    with _PLATE_PICTURE_LOCK:
        if key in _PLATE_PICTURE_CACHE:
            _PLATE_PICTURE_CACHE.move_to_end(key)
            return _PLATE_PICTURE_CACHE[key]

    entry: str | None = None
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            entry = plate_picture_entry(zf, plate_id)
    except (OSError, zipfile.BadZipFile):
        entry = None

    with _PLATE_PICTURE_LOCK:
        _PLATE_PICTURE_CACHE[key] = entry
        _PLATE_PICTURE_CACHE.move_to_end(key)
        while len(_PLATE_PICTURE_CACHE) > _PLATE_PICTURE_MAX:
            _PLATE_PICTURE_CACHE.popitem(last=False)
    return entry


def plate_picture_for_row(*, plate_id: int | None, descriptor: QueueSourceDescriptor | None) -> str | None:
    """The entry holding the picture of a job's OWN captured bytes, or ``None``.

    ``None`` for a legacy row — it has no snapshot, so its picture still comes
    from whichever original row it names — and ``None`` for a snapshot whose
    object is gone, broken, or carries no render of this plate. Never a fall back
    to another file (S7) and never to another plate.

    The plate precedence is the one every other snapshot reader uses:
    ``plate_id or descriptor.plate_fallback``.
    """
    if descriptor is None:
        return None
    return plate_picture_cached(descriptor.path, plate_id or descriptor.plate_fallback)


def plate_metadata_for_row(
    *, plate_id: int | None, descriptor: QueueSourceDescriptor | None
) -> tuple[int | None, float, str | None]:
    """``(print_time, filament_grams, bed_type)`` of a job's own captured bytes (m173).

    The snapshot is the source of truth for what a queued job IS (spec §4, A09):
    its original row may be gone, and while it exists it may have been re-sliced
    since — A03 says the job keeps the bytes it accepted, so the card has to
    describe those. One call, three values, because the three parsers share one
    cache entry.

    **Where the answer is cached:** the module-level LRU above, keyed by the file's
    own revision — the same one the original path has always used. For a
    content-addressed immutable object that key can never go stale, so a queue poll
    opens the ZIP once per (object, plate) and every later poll of every row is a
    dict lookup.

    ``(None, 0.0, None)`` for a job with no snapshot, and for one whose object is
    missing or unreadable — never a fall back to another file (S7).
    """
    if descriptor is None:
        return None, 0.0, None
    return plate_metadata_cached(descriptor.path, plate_id or descriptor.plate_fallback)


def _recorded_estimate(archive: PrintArchive | None, library_file: LibraryFile | None) -> int | None:
    """The estimate already stored on a row — column reads only, no disk access."""
    if archive is not None and archive.deleted_at is None:
        return archive.print_time_seconds
    if library_file is not None:
        seconds = (library_file.file_metadata or {}).get("print_time_seconds")
        return int(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
    return None


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

    ⚠️ **A job with a snapshot never has its ORIGINAL FILE re-read for this.** When
    the object says nothing — no ``prediction`` on that plate, or the object is
    missing or ``broken`` — the fallback is the row's recorded *column*, which was
    read out of the bytes the job accepted and is therefore about the right file.
    The original's file on disk is not: it may have been re-sliced since (A03), and
    showing a number out of bytes this job will not print is the exact thing this
    feature exists to stop. ``filaments_for_row`` refuses in the same case, and the
    two readers must agree. A job with neither answers ``None``.
    """
    if descriptor is not None:
        facts = plate_metadata_for_row(plate_id=plate_id, descriptor=descriptor)
        if facts[0] is not None:
            return facts[0]
        return _recorded_estimate(archive, library_file)
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

    ⚠️ Both file-reading branches go through the **revision-keyed cache**, and for
    this reader that is not an optimisation but a correctness-of-cost matter: its
    only caller asks it once per PENDING ROW of every active order, on a polled
    page, so a quantity-20 line queued from one file meant twenty opens of the same
    ZIP per request. The cache makes it one per (object, plate) — and for a
    captured object that entry is valid for ever, the key being content-addressed.
    """
    if descriptor is not None:
        return plate_filaments_cached(descriptor.path, plate_id or descriptor.plate_fallback)
    if archive is not None and archive.deleted_at is None:
        path = settings.base_dir / archive.file_path
        return plate_filaments_cached(path, plate_id) if archive.file_path else None
    if library_file is not None:
        filaments = plate_filaments(library_file.file_metadata, plate_id or 0)
        return filaments or None
    return None
