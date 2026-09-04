"""``scripts/prune_orphan_archive_files.py`` — what it deletes, and what it must not.

The script reconciles ``<DATA_DIR>/archive/`` against the file columns of
``print_archives`` and ``library_files``. Two things brought it here:

* ``delete_project`` and ``delete_product`` remove the row and leave
  ``archive/{projects,products}/<id>/attachments/`` on disk, so a deleted
  order's pictures outlive it — nothing swept those;
* ⚠️ and those same directories were inside the FILE sweep, where no row can
  ever name them, so ``--apply`` on a healthy install would have deleted every
  LIVE attachment as an orphan.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import prune_orphan_archive_files as prune  # noqa: E402


def _data_dir(tmp_path: Path, *, projects=(1,), products=(1,), with_tables=True) -> Path:
    data = tmp_path / "data"
    (data / "archive").mkdir(parents=True)
    conn = sqlite3.connect(data / "bamdude.db")
    conn.execute("CREATE TABLE print_archives (file_path TEXT, thumbnail_path TEXT)")
    conn.execute("CREATE TABLE library_files (file_path TEXT, thumbnail_path TEXT)")
    conn.execute("INSERT INTO print_archives VALUES ('archive/kept/a.3mf', 'archive/kept/a.png')")
    if with_tables:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO projects (id) VALUES (?)", [(i,) for i in projects])
        conn.executemany("INSERT INTO products (id) VALUES (?)", [(i,) for i in products])
    conn.commit()
    conn.close()
    return data


def _write(path: Path, body: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _populate(data: Path) -> dict[str, Path]:
    archive = data / "archive"
    return {
        "referenced": _write(archive / "kept" / "a.3mf"),
        "orphan": _write(archive / "stray" / "b.3mf"),
        "live_attachment": _write(archive / "projects" / "1" / "attachments" / "cover.png"),
        "dead_order": _write(archive / "projects" / "9" / "attachments" / "cover.png"),
        "live_product": _write(archive / "products" / "1" / "attachments" / "bom.csv"),
        "dead_product": _write(archive / "products" / "7" / "attachments" / "bom.csv"),
    }


def test_a_dry_run_reports_the_orphans_and_touches_nothing(tmp_path, capsys):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data)]) == 0

    out = capsys.readouterr().out
    assert "Orphan attachment directories: 2" in out
    assert str(data / "archive" / "projects" / "9") in out
    assert str(data / "archive" / "products" / "7") in out
    assert all(p.exists() for p in paths.values()), "a dry run deleted something"


def test_apply_removes_the_dead_directories_and_keeps_the_live_ones(tmp_path):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    assert paths["referenced"].exists()
    assert not paths["orphan"].exists(), "an unreferenced archive file is still an orphan"
    # ⚠️ The whole point: a live order's and a live product's attachments are
    # not archive files, and are not orphans either.
    assert paths["live_attachment"].exists()
    assert paths["live_product"].exists()
    assert not (data / "archive" / "projects" / "9").exists()
    assert not (data / "archive" / "products" / "7").exists()


def test_a_database_without_the_tables_sweeps_neither_subtree(tmp_path, capsys):
    """ "Cannot tell" is not "nothing is referenced".

    An older database has no ``products`` table at all. Reading that as an
    empty id set would delete every attachment the install has.
    """
    data = _data_dir(tmp_path, with_tables=False)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    err = capsys.readouterr().err
    assert "cannot read projects" in err and "cannot read products" in err
    assert all(p.exists() for key, p in paths.items() if key != "orphan")


def test_a_directory_that_is_not_an_id_is_left_alone(tmp_path, capsys):
    data = _data_dir(tmp_path)
    stray = _write(data / "archive" / "projects" / "notanid" / "attachments" / "x.png")

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    assert stray.exists()
    assert "not an id, left alone" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["bamdude.db", "archive"])
def test_it_refuses_to_guess_when_the_data_dir_is_not_one(tmp_path, missing):
    data = _data_dir(tmp_path)
    target = data / ("bamdude.db" if missing == "bamdude.db" else "archive")
    if missing == "bamdude.db":
        target.unlink()
        assert prune.main(["--data-dir", str(data)]) == 1
    else:
        target.rmdir()
        assert prune.main(["--data-dir", str(data)]) == 0
