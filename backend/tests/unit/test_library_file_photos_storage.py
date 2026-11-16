"""Where a library file's photos live, and the two things that walk the disk
without asking the application: the backup and the orphan pruner.

The photos sit under the library's root (``<library>/photos/<file id>/``), so a
backup carries them as part of the library. The pruner reconciles
``<DATA_DIR>/library/`` against the database's file columns — and no ``*_path``
column names a photo, so until it was taught the folder it would have called
every live photo an orphan and, with ``--apply``, deleted them all.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import prune_orphan_archive_files as prune  # noqa: E402

from backend.app.services import backup_files, library_file_photos as photos  # noqa: E402

STEM_A = "a" * 32
STEM_B = "b" * 32
STEM_GONE = "c" * 32


def _write(path: Path, body: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# -- backup ----------------------------------------------------------------------


def test_the_photo_folder_is_inside_a_root_the_backup_carries(tmp_path, monkeypatch):
    from backend.app.core.config import settings

    library = Path(settings.library_dir).resolve()
    assert backup_files.directories(settings)["library"].resolve() == library
    assert library in photos.photos_dir(7).resolve().parents
    assert photos.photos_dir(7).resolve() == library / photos.PHOTOS_DIRECTORY / "7"


def test_staging_a_backup_copies_the_photos(tmp_path):
    data = tmp_path / "data"
    library = data / "library"
    photo = _write(library / photos.PHOTOS_DIRECTORY / "7" / f"{STEM_A}.jpg", b"photo-bytes")
    small = _write(library / photos.PHOTOS_DIRECTORY / "7" / f"{STEM_A}.sm.jpg", b"small-bytes")
    _write(library / "files" / "model.3mf", b"model")
    settings = SimpleNamespace(
        base_dir=data,
        archive_dir=data / "archive",
        library_dir=library,
        plate_calibration_dir=data / "plate_calibration",
        projects_dir=data / "projects",
        products_dir=data / "products",
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    backup_files.stage_files(settings, data, staging)

    copied = staging / "library" / photos.PHOTOS_DIRECTORY / "7"
    assert (copied / photo.name).read_bytes() == b"photo-bytes"
    assert (copied / small.name).read_bytes() == b"small-bytes"


# -- the orphan pruner ---------------------------------------------------------------


def _data_dir(tmp_path: Path, *, with_photo_table: bool = True) -> Path:
    data = tmp_path / "data"
    (data / "archive").mkdir(parents=True)
    conn = sqlite3.connect(data / "bamdude.db")
    conn.execute("CREATE TABLE print_archives (file_path TEXT, thumbnail_path TEXT)")
    conn.execute("CREATE TABLE library_files (id INTEGER PRIMARY KEY, file_path TEXT, thumbnail_path TEXT)")
    conn.execute("INSERT INTO library_files VALUES (7, 'library/files/live.3mf', NULL)")
    conn.execute("INSERT INTO library_files VALUES (8, 'library/files/other.3mf', NULL)")
    if with_photo_table:
        conn.execute(
            "CREATE TABLE library_file_photos (id INTEGER PRIMARY KEY, library_file_id INTEGER, stored_name TEXT)"
        )
        conn.execute("INSERT INTO library_file_photos VALUES (1, 7, ?)", (f"{STEM_A}.jpg",))
        conn.execute("INSERT INTO library_file_photos VALUES (2, 7, ?)", (f"{STEM_B}.png",))
    conn.commit()
    conn.close()
    return data


def _populate(data: Path) -> dict[str, Path]:
    root = data / "library" / "photos"
    return {
        "file": _write(data / "library" / "files" / "live.3mf"),
        "other_file": _write(data / "library" / "files" / "other.3mf"),
        "photo": _write(root / "7" / f"{STEM_A}.jpg"),
        "photo_small": _write(root / "7" / f"{STEM_A}.sm.jpg"),
        "second": _write(root / "7" / f"{STEM_B}.png"),
        "second_small": _write(root / "7" / f"{STEM_B}.sm.jpg"),
        # Left by an upload that never got its row: nothing names it.
        "unnamed": _write(root / "7" / f"{STEM_GONE}.jpg"),
        # The same NAME under another file's folder is not that file's photo.
        "wrong_folder": _write(root / "8" / f"{STEM_A}.jpg"),
        # A folder whose file was purged.
        "dead_folder": _write(root / "99" / f"{STEM_A}.jpg"),
    }


def test_a_live_files_photos_survive_the_pruner_and_orphans_do_not(tmp_path):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    for kept in ("file", "other_file", "photo", "photo_small", "second", "second_small"):
        assert paths[kept].exists(), f"{kept} was deleted"
    for swept in ("unnamed", "wrong_folder", "dead_folder"):
        assert not paths[swept].exists(), f"{swept} is still there"
    assert not (data / "library" / "photos" / "99").exists(), "an emptied folder is collapsed"


def test_a_dry_run_touches_no_photo(tmp_path, capsys):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data)]) == 0

    assert all(p.exists() for p in paths.values())
    out = capsys.readouterr().out
    assert str(paths["dead_folder"]) in out
    assert str(paths["photo"]) not in out


def test_a_database_without_the_photo_table_leaves_the_photo_folder_alone(tmp_path, capsys):
    """An older database cannot say which photos are wanted: "cannot tell" is
    not "nothing is referenced"."""
    data = _data_dir(tmp_path, with_photo_table=False)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    photo_paths = [p for name, p in paths.items() if name not in ("file", "other_file")]
    assert all(p.exists() for p in photo_paths), "photos were swept on a database that cannot name them"
    assert "library_file_photos" in capsys.readouterr().err
