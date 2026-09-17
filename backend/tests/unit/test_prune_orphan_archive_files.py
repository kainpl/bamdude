"""``scripts/prune_orphan_archive_files.py`` — what it deletes, and what it must not.

The script reconciles ``<DATA_DIR>/archive/`` and ``<DATA_DIR>/library/``
against the file columns of ``print_archives`` and ``library_files`` — two file
roots, one reference set, because those two tables name files under both
(vault 40-invariants/inv-data-dir-one-root-per-subsystem). Two things brought
it here:

* ``delete_project`` and ``delete_product`` remove the row and leave
  ``{projects,products}/<id>/attachments/`` on disk, so a deleted order's
  pictures outlive it — nothing swept those;
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
    conn.execute("INSERT INTO library_files VALUES ('library/files/c.3mf', NULL)")
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
        # The library is its own root since m177 and is swept against the same
        # reference set - a file nothing names there is an orphan too.
        "library_referenced": _write(data / "library" / "files" / "c.3mf"),
        "library_orphan": _write(data / "library" / "files" / "stray.3mf"),
        "live_attachment": _write(data / "projects" / "1" / "attachments" / "cover.png"),
        "dead_order": _write(data / "projects" / "9" / "attachments" / "cover.png"),
        "live_product": _write(data / "products" / "1" / "attachments" / "bom.csv"),
        "dead_product": _write(data / "products" / "7" / "attachments" / "bom.csv"),
    }


def test_a_dry_run_reports_the_orphans_and_touches_nothing(tmp_path, capsys):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data)]) == 0

    out = capsys.readouterr().out
    assert "Orphan attachment directories: 2" in out
    assert str(data / "projects" / "9") in out
    assert str(data / "products" / "7") in out
    assert all(p.exists() for p in paths.values()), "a dry run deleted something"


def test_apply_removes_the_dead_directories_and_keeps_the_live_ones(tmp_path):
    data = _data_dir(tmp_path)
    paths = _populate(data)

    assert prune.main(["--data-dir", str(data), "--apply"]) == 0

    assert paths["referenced"].exists()
    assert not paths["orphan"].exists(), "an unreferenced archive file is still an orphan"
    assert paths["library_referenced"].exists()
    assert not paths["library_orphan"].exists(), "the library root is swept against the same rows"
    # ⚠️ The whole point: a live order's and a live product's attachments are
    # not archive files, and are not orphans either.
    assert paths["live_attachment"].exists()
    assert paths["live_product"].exists()
    assert not (data / "projects" / "9").exists()
    assert not (data / "products" / "7").exists()


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
    assert all(p.exists() for key, p in paths.items() if key not in ("orphan", "library_orphan"))


def test_a_directory_that_is_not_an_id_is_left_alone(tmp_path, capsys):
    data = _data_dir(tmp_path)
    stray = _write(data / "projects" / "notanid" / "attachments" / "x.png")

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


class TestItRefusesWhatItCannotAnswer:
    """⚠️ The script deletes what no database row names. So a database that
    names nothing is not an empty install — it is a broken question, and the
    honest answer to it is to stop. Same shape as ``EMPTY_WALK_GUARD`` in
    ``services/library_scan.py``.

    Both of these used to print a warning and then report every file under
    ``archive/`` as an orphan, with ``--apply`` offered on the next line.
    """

    def test_an_empty_leftover_database_stops_the_run(self, tmp_path, monkeypatch):
        """What a PostgreSQL install actually looks like on disk: a 0-byte
        ``data/bamdude.db`` beside ``bamdude.db.migrated``. SQLite opens it
        happily as a valid empty database."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        data = tmp_path / "data"
        (data / "archive").mkdir(parents=True)
        (data / "bamdude.db").touch()
        _write(data / "archive" / "keep" / "a.3mf")

        with pytest.raises(SystemExit) as excinfo:
            prune.main(["--data-dir", str(data)])
        assert "print_archives" in str(excinfo.value)
        assert (data / "archive" / "keep" / "a.3mf").exists()

    def test_it_will_not_run_against_a_postgresql_install(self, tmp_path, monkeypatch):
        """The rows are in PostgreSQL; whatever SQLite file is lying around
        cannot say what is referenced."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://bamdude@db.local/bamdude")
        data = _data_dir(tmp_path)
        files = _populate(data)

        with pytest.raises(SystemExit) as excinfo:
            prune.main(["--data-dir", str(data)])
        assert "DATABASE_URL" in str(excinfo.value)
        assert files["orphan"].exists()

    def test_a_healthy_sqlite_install_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        data = _data_dir(tmp_path)
        _populate(data)
        assert prune.main(["--data-dir", str(data)]) == 0
