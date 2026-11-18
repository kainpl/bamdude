"""Where a finish photo is written, decided at the moment of the write (G10 G, M30).

The finish photo of a print is captured by one of several sources — the last
frame of the printer's own timelapse, a frame banked at the last layer, the
live view's buffer, a fresh grab — and each used to work out the photo folder
from an archive row read at the START of the task, tens of seconds before the
write. Two things were left open:

* the folder of an archive can change meanwhile (a 3MF attached late moves it),
  and the row itself can stop being that archive: the id of a deleted archive
  is handed to the next one;
* a photo written and then refused at the final check (the archive changed, or
  the printer went on to another print) stayed on disk, named by nothing.

So every writer asks here, immediately before it writes:
:func:`photos_dir_at_write` reads the row again and answers its folder only
when it is still the very archive the terminal was for (``recovery_identity``),
and :func:`discard` removes a photo that was written and then not attached.

No camera is touched here and no printer command is sent: what triggers a
capture, and from which source, is unchanged (owner decision 2026-09-14: no
motion for the sake of a photo).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def new_name() -> str:
    """A photo name of ours: a time for people, eight random hex digits against a collision."""
    return f"finish_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"


async def photos_dir_at_write(archive_id: int, expected_identity: str | None, *, session: Callable) -> Path | None:
    """The folder this archive's photos are written to NOW, created — or ``None`` when it is not that archive any more."""
    from backend.app.models.archive import PrintArchive
    from backend.app.utils.archive_paths import photos_dir_for

    async with session() as db:
        archive = await db.get(PrintArchive, archive_id)
        if archive is None or (expected_identity is not None and archive.recovery_identity != expected_identity):
            return None
        folder = photos_dir_for(archive)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    return folder


async def save_frame(
    archive_id: int,
    expected_identity: str | None,
    data: bytes,
    *,
    session: Callable,
    may_run: Callable[[], bool] | None = None,
) -> str | None:
    """Write one captured frame as a finish photo of that very archive. ``None`` = nothing was written.

    ``may_run`` is the terminal's own proof that the printer has not gone on to
    another print; it is asked after the folder was read and before the write.
    """
    folder = await photos_dir_at_write(archive_id, expected_identity, session=session)
    if folder is None or (may_run is not None and not may_run()):
        return None
    name = new_name()
    await asyncio.to_thread((folder / name).write_bytes, data)  # SEC-PATH-OK: ``name`` is generated above
    return name


async def discard(archive_id: int, filename: str | None, *, session: Callable) -> None:
    """Remove a finish photo that was written and then not attached. Best effort; never raises."""
    if not filename:
        return
    from backend.app.models.archive import PrintArchive
    from backend.app.utils.archive_paths import find_photo

    try:
        async with session() as db:
            archive = await db.get(PrintArchive, archive_id)
            # A row that is gone took its folders with it (``ArchiveService.delete_archive``).
            path = find_photo(archive, filename) if archive is not None else None
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — a leftover photo is an orphan file, not a failed print
        logger.debug("A refused finish photo of archive %s could not be removed: %s", archive_id, type(exc).__name__)
