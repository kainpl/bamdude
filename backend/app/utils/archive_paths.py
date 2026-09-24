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
(upstream #1820). The timelapse and design-file writers still derived it by hand
until 2026-09-24 and wrote one folder ABOVE the data directory (upstream #2843).

⚠️ **The fallback is ``archive/no_source/<id>/``, not ``archive/<id>/``**
(audit 1.2.5.3-1.2.5.6, D14). Printer folders are ``archive/<printer id>/``, so
the old fallback put archive 3's files inside printer 3's folder, and deleting
them with the row was not safe — so they were never deleted. ``no_source/<id>``
is where a source 3MF uploaded onto such an archive already went.

⚠️ **A photo's folder is not fixed for the life of an archive.** Only the file
NAME is stored, and the folder changes the moment a 3MF is attached to an
archive that had none — which is routine: the finish photo is taken at
completion, the 3MF can arrive later from the retry sweep. So photos are READ
through :func:`find_photo`, which looks in every folder a photo of this archive
can have been written to; they are always WRITTEN to :func:`photos_dir_for`.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.core.config import settings
from backend.app.utils.safe_path import safe_join_under


def fallback_dir_for(archive_id: int) -> Path:
    """The folder an archive owns while it has no 3MF — and keeps owning after."""
    return settings.archive_dir / "no_source" / str(archive_id)  # SEC-PATH-OK: int primary key


def legacy_photos_dir_for(archive_id: int) -> Path:
    """Where photos of a no-3MF archive went before 2026-09-24.

    Read-only: nothing writes here any more. Safe to remove with the archive —
    ``archive/<n>/photos`` can only ever have been written for archive *n*
    (archive folders under a printer are dated, never ``photos``) — but its
    parent ``archive/<n>/`` may be printer *n*'s folder and must be left alone.
    """
    return settings.archive_dir / str(archive_id) / "photos"  # SEC-PATH-OK: int primary key


def archive_dir_for(archive) -> Path:
    """The folder holding *archive*'s files — 3MF, thumbnail, timelapse, photos.

    The 3MF's folder when the row has one, :func:`fallback_dir_for` otherwise.
    """
    if archive.file_path:
        return settings.base_dir / Path(archive.file_path).parent
    return fallback_dir_for(archive.id)


def photos_dir_for(archive) -> Path:
    """Where this archive's photos are WRITTEN. Read them with :func:`find_photo`."""
    return archive_dir_for(archive) / "photos"


def photo_dirs_for(archive) -> list[Path]:
    """Every folder a photo of *archive* can have been written to, current first."""
    dirs = [photos_dir_for(archive), fallback_dir_for(archive.id) / "photos", legacy_photos_dir_for(archive.id)]
    return list(dict.fromkeys(dirs))


def find_photo(archive, filename: str, *, http: bool = False) -> Path | None:
    """The file behind a stored photo name, wherever it was written, or None.

    ``filename`` is contained by ``safe_join_under`` in each folder — it reaches
    here from an unauthenticated URL — so a traversal name raises (a 400 with
    ``http=True``, for routes) rather than being looked up anywhere.
    """
    candidates = [safe_join_under(folder, filename, http=http) for folder in photo_dirs_for(archive)]
    return next((path for path in candidates if path.exists()), None)
