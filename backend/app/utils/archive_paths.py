"""Where an archive's files live on disk.

One answer, because there were two and they disagreed.

⚠️ **An archive can legitimately have no ``file_path``.** The row is created at
PRINT START, before the 3MF is downloaded, and stays empty when there is nothing
to download — a job started from a printer's own internal library never yields
one. That is a normal state here, not a corrupt row.

``Path("").parent`` is ``Path(".")``, so deriving the folder from an empty
``file_path`` resolves to the **data directory itself**, and anything written
into it lands in ``<DATA_DIR>/photos`` rather than under the archive. The
finish-photo background writer noticed and grew its own fallback; the three
photo endpoints did not. So a photo captured automatically went one place, a
photo uploaded by hand went another, and each endpoint looked in the wrong one
(upstream #1820).
"""

from __future__ import annotations

from pathlib import Path

from backend.app.core.config import settings


def archive_dir_for(archive) -> Path:
    """The folder holding *archive*'s files — 3MF, thumbnail, timelapse, photos.

    Falls back to ``<archive_dir>/<id>`` when the row carries no ``file_path``,
    which is the shape the finish-photo writer already used and therefore the
    one that keeps existing captures findable.
    """
    if archive.file_path:
        return settings.base_dir / Path(archive.file_path).parent
    return settings.archive_dir / str(archive.id)


def photos_dir_for(archive) -> Path:
    """Where this archive's photos are kept."""
    return archive_dir_for(archive) / "photos"
