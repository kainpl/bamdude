"""Sweep the archive/ directory for files + dirs not referenced by any DB row.

Background
----------
After the 0.4.x patch-pipeline rework, on-disk archive copies are exclusively
the **unpatched** source bytes — patched copies live only in /tmp during the
FTP upload window and are cleaned up by the dispatcher. ``archive_print`` and
``attach_3mf_to_archive`` both dedup on the chain-root hash, so two patched
variants of the same source share one on-disk file.

Existing installs may still carry leftovers from the prior semantics:

* per-print archive_dirs that held a patched copy of every dispatch
* directories left behind when an archive row was hard-deleted but a stale
  reference (or a delete that ran before the chain-share check shipped)
  prevented the rmtree
* files dropped into archive/ manually by an operator

This script reconciles on-disk state against the DB. A regular file under
``<DATA_DIR>/archive/`` or ``<DATA_DIR>/library/`` is wanted when

* any ``*_path`` column of ``print_archives``, ``library_files`` or
  ``library_file_makerworld_meta`` names it (read off the schema, so a new file
  column is covered by its name), or
* it lies in a folder an archive row owns whole — its 3MF's folder, its
  ``no_source/<id>/`` fallback, or the older ``<id>/photos/`` — because photos
  are stored as bare names and an edited timelapse is named by nothing.

…across **both live and trashed** rows (``deleted_at`` IS NULL or NOT NULL):
trash-retained files still live on disk until the retention sweeper hard-
deletes them. Everything else is an orphan. After files are removed, empty
directories are collapsed bottom-up.

⚠️ Until 2026-09-24 only ``file_path`` + ``thumbnail_path`` counted, so
``--apply`` deleted every timelapse, photo, Fusion design, source 3MF and
MakerWorld cover (audit 1.2.5.3-1.2.5.6, D14).

Skipped from the file sweep:

* ``<DATA_DIR>/archive/temp/`` — runtime FTP staging area, rebuilt per upload
* ``<DATA_DIR>/archive/projects/`` and ``<DATA_DIR>/archive/products/`` — an
  order's or a product's attachments, covered by the directory sweep below
  instead. ⚠️ They are NOT archive files: no row of ``print_archives`` or
  ``library_files`` names them, so the file sweep would call every live
  attachment an orphan and ``--apply`` would delete the lot.
* anything outside ``<DATA_DIR>/archive/`` (the database's ``file_path`` is
  always relative to ``DATA_DIR``, but only ``archive/`` is BamDude's
  responsibility — leave certs, virtual_printer alone).

Attachment directories
----------------------
``delete_project`` and ``delete_product`` remove the row and leave
``archive/{projects,products}/<id>/attachments/`` on disk, so a deleted order's
pictures outlive it. The second pass here lists every ``<id>`` directory under
those two whose row is gone and, with ``--apply``, removes it whole.

⚠️ The id set is read from the ``projects`` / ``products`` tables, and a
database that HAS NO such table means "cannot tell", never "nothing is
referenced": that subtree is skipped with a warning rather than swept. A
directory whose name is not an integer is reported and left alone for the same
reason.

Usage
-----
.. code-block:: bash

    # See what would be deleted (default — read-only):
    python scripts/prune_orphan_archive_files.py

    # Actually delete:
    python scripts/prune_orphan_archive_files.py --apply

    # Custom data dir (defaults to env DATA_DIR or <repo>/data):
    python scripts/prune_orphan_archive_files.py --data-dir /var/lib/bamdude

PostgreSQL-backed installs are not supported by this script — use a direct
SQL query against ``print_archives`` + ``library_files`` to build the
reference set yourself, or temporarily run a SQLite copy of the DB.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# The tables whose file columns decide what is referenced. If either is
# unreadable the answer to "what is an orphan?" is "everything".
_REQUIRED_TABLES = ("print_archives", "library_files")


def _refuse_unusable_database(db_path: Path) -> None:
    """Stop before the scan when the database cannot answer the question.

    ⚠️ Two ways this script used to arrive at "delete everything", both silent:

    * **The install is on PostgreSQL.** ``DATABASE_URL`` points elsewhere and
      this SQLite file is a leftover — on a migrated install it is the 0-byte
      ``data/bamdude.db`` the app recreates beside ``bamdude.db.migrated``.
    * **The file exists but holds no schema.** ``Path.exists()`` was the only
      check, and SQLite opens an empty file as a valid empty database.

    Either way the two SELECTs below raised "no such table", which was caught
    and printed as a warning, the referenced set stayed empty, and every file
    under ``archive/`` was reported as an orphan — with ``--apply`` offered on
    the next line. Same shape as the ``EMPTY_WALK_GUARD`` in
    ``services/library_scan.py``: an empty answer from a stocked directory is a
    broken question, not a mandate to delete.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        where = "the bundled PostgreSQL" if database_url == "embedded" else database_url.split("@")[-1]
        raise SystemExit(
            f"refusing to run: DATABASE_URL is set ({where}), so this install's data is not in "
            f"{db_path}.\nThis script reads SQLite only. Against a leftover SQLite file it would "
            "report every archived file as an orphan."
        )

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in _REQUIRED_TABLES if name not in present]
    if missing:
        raise SystemExit(
            f"refusing to run: {db_path} has no {', '.join(missing)} table.\n"
            "That is not a BamDude database (or it is an empty leftover). Every file would look "
            "unreferenced, and --apply would delete all of them."
        )


def _resolve_data_dir(arg_data_dir: str | None) -> Path:
    if arg_data_dir:
        return Path(arg_data_dir).resolve()
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env).resolve()
    return (Path(__file__).resolve().parent.parent / "data").resolve()


# Tables whose ``*_path`` columns name files under ``archive/`` or ``library/``.
# ⚠️ EVERY such column, read off the table itself: listing them by hand is how
# this script came to know only ``file_path`` + ``thumbnail_path`` — and with
# ``--apply`` delete every timelapse, Fusion design, source 3MF and MakerWorld
# cover (audit 1.2.5.3-1.2.5.6, D14). A new file column is covered by its name.
# The first two are required (see ``_REQUIRED_TABLES``); the MakerWorld one is
# read when present.
_PATH_TABLES = ("print_archives", "library_files", "library_file_makerworld_meta")


def _path_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if str(row[1]).endswith("_path")]  # noqa: S608 — fixed tables


def _collect_referenced_paths(db_path: Path) -> set[str]:
    """Build the set of relative paths (POSIX style, from DATA_DIR) the DB references.

    Includes trashed rows so files in the trash retention window aren't yanked
    from under the sweeper. POSIX-normalised because the DB stores forward-
    slash paths regardless of host OS, and on Windows `Path("a\\b").as_posix()`
    gives "a/b" — keeping comparison side-agnostic.
    """
    conn = sqlite3.connect(str(db_path))
    referenced: set[str] = set()
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _PATH_TABLES:
        if table not in present and table not in _REQUIRED_TABLES:
            continue
        try:
            cols = _path_columns(conn, table)
            if not cols:
                raise sqlite3.OperationalError(f"no *_path column on {table}")
            cols_sql = ", ".join(cols)
            for row in conn.execute(f"SELECT {cols_sql} FROM {table}"):  # noqa: S608 — columns read off the schema
                for v in row:
                    if v:
                        referenced.add(Path(str(v)).as_posix())
        except sqlite3.OperationalError as e:
            # ⚠️ NOT a warning. An unreadable table means "nothing is
            # referenced", and this script deletes precisely what is not
            # referenced — so the friendly degradation was a full wipe of the
            # archive. See ``_refuse_unusable_database`` for the guard that
            # normally stops us reaching here.
            conn.close()
            raise SystemExit(
                f"refusing to continue: cannot read {table} ({e}).\n"
                "Every file would look unreferenced, and --apply would delete all of them."
            ) from e
    conn.close()
    return referenced


def _owned_archive_dirs(db_path: Path, data_dir: Path) -> list[Path]:
    """Folders a live (or trashed) archive owns WHOLE, so nothing inside them is an orphan.

    Not every file of an archive is named by a column: its photos are stored as
    bare names, and the timelapse editor's "save as new" copy is named by
    nothing. So an archive owns (mirrors ``backend/app/utils/archive_paths.py``):

    * the folder of its 3MF — photos/, source/, f3d/, edited timelapses;
    * ``archive/no_source/<id>/`` — its folder while it had no 3MF;
    * ``archive/<id>/photos/`` — the fallback's photos before 2026-09-24.

    ⚠️ Never ``archive/<id>/`` itself: that is also printer <id>'s folder. And a
    3MF folder is owned only when it is at least two levels into ``archive/``
    (``archive/<printer>/<dated folder>``): a row whose file lies higher would
    otherwise claim a whole printer's folder, or the whole archive.
    """
    archive_root = data_dir / "archive"
    owned: list[Path] = []
    with closing(sqlite3.connect(str(db_path))) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(print_archives)")}
        select_id = "id" if "id" in columns else "NULL"
        for archive_id, file_path in conn.execute(f"SELECT {select_id}, file_path FROM print_archives"):  # noqa: S608 — fixed columns
            if file_path:
                folder = (data_dir / Path(str(file_path))).parent
                try:
                    depth = len(folder.relative_to(archive_root).parts)
                except ValueError:
                    depth = 0
                if depth >= 2:
                    owned.append(folder)
            if isinstance(archive_id, int):
                owned.append(archive_root / "no_source" / str(archive_id))
                owned.append(archive_root / str(archive_id) / "photos")
    return owned


# ``<data_dir>/<subdir>/<id>/`` — one per row of ``<table>``, attachments inside
# (their own roots since m177; before that they sat under archive/).
_ENTITY_DIRS: tuple[tuple[str, str], ...] = (("projects", "projects"), ("products", "products"))


def _orphan_entity_dirs(db_path: Path, data_dir: Path) -> list[Path]:
    """``<data_dir>/{projects,products}/<id>`` directories whose row is gone.

    Returns them deepest-safe (whole directory, attachments and all) — the row
    is what made the directory meaningful, so nothing inside it can be wanted.
    """
    orphans: list[Path] = []
    # ``closing``: anything raised below would otherwise walk out of this
    # function with the connection still open — and the caller goes on to delete
    # files, which on Windows a lingering handle can refuse.
    with closing(sqlite3.connect(str(db_path))) as conn:
        for subdir, table in _ENTITY_DIRS:
            root = data_dir / subdir
            if not root.exists():
                continue
            try:
                live = {int(row[0]) for row in conn.execute(f"SELECT id FROM {table}")}  # noqa: S608 — fixed tables
            except (sqlite3.OperationalError, TypeError, ValueError) as e:
                # No such table (an older database) is "cannot tell", not "nothing
                # is referenced". Sweeping on that reading would delete every
                # attachment the install has.
                print(f"warning: skipping {root}: cannot read {table}: {e}", file=sys.stderr)
                continue
            try:
                children = sorted(root.iterdir())
            except OSError as e:
                # A root we cannot LIST is "cannot tell", the same reading the
                # missing-table branch above takes: an offline mount or a
                # permission change must not be reported as "nothing here is
                # referenced". Skipping it leaves its subtree alone; the caller
                # only ever deletes what this function returns.
                print(f"warning: skipping {root}: cannot list it: {e}", file=sys.stderr)
                continue
            for child in children:
                if not child.is_dir():
                    continue
                if not child.name.isdigit():
                    print(f"  not an id, left alone: {child}")
                    continue
                if int(child.name) not in live:
                    orphans.append(child)
    return orphans


def _is_under(path: Path, ancestor: Path) -> bool:
    """True when ``path`` equals ``ancestor`` or is somewhere beneath it."""
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _walk_archive_files(archive_root: Path, data_dir: Path, skip_dirs: list[Path]):
    """Yield (relative_posix_path, abs_path) for regular files under one root
    (``archive/`` or, since m177, ``library/``).

    ``skip_dirs`` are absolute paths to omit (anything under them is skipped).
    """
    if not archive_root.exists():
        return
    for root, dirs, files in os.walk(archive_root):
        root_p = Path(root)
        # Prune walk so we don't even descend into skipped subtrees.
        dirs[:] = [d for d in dirs if not any(_is_under(root_p / d, sd) for sd in skip_dirs)]
        if any(_is_under(root_p, sd) for sd in skip_dirs):
            continue
        for fname in files:
            abs_path = root_p / fname
            try:
                rel = abs_path.relative_to(data_dir).as_posix()
            except ValueError:
                continue
            yield rel, abs_path


def _collapse_empty_dirs(archive_root: Path, skip_dirs: list[Path], dry_run: bool) -> int:
    """Remove every empty directory under archive_root (deepest first). Returns count."""
    if not archive_root.exists():
        return 0
    removed = 0
    # topdown=False walks deepest dirs first, which is exactly what we need
    # so emptying a parent only after its children were processed.
    for root, _dirs, _files in os.walk(archive_root, topdown=False):
        root_p = Path(root)
        if root_p == archive_root:
            continue  # never delete the archive root itself
        if any(_is_under(root_p, sd) for sd in skip_dirs):
            continue
        try:
            if not any(root_p.iterdir()):
                if dry_run:
                    print(f"  would remove empty dir: {root_p}")
                else:
                    root_p.rmdir()
                    print(f"  removed empty dir: {root_p}")
                removed += 1
        except OSError as e:
            print(f"  error checking/removing {root_p}: {e}", file=sys.stderr)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prune_orphan_archive_files", description=__doc__)
    parser.add_argument("--data-dir", help="Override DATA_DIR (default: $DATA_DIR or <repo>/data)")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    args = parser.parse_args(argv)

    data_dir = _resolve_data_dir(args.data_dir)
    db_path = data_dir / "bamdude.db"
    archive_root = data_dir / "archive"
    library_root = data_dir / "library"  # its own root since m177
    # ⚠️ ``projects`` and ``products`` are skipped by the FILE sweep on purpose:
    # nothing in the database's file columns names an attachment, so every live
    # one would read as an orphan. They get their own directory pass below.
    skip_dirs = [archive_root / "temp", archive_root / "projects", archive_root / "products"]

    if not db_path.exists():
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 1
    _refuse_unusable_database(db_path)
    if not archive_root.exists():
        print(f"archive/ not found at {archive_root} — nothing to do")
        return 0

    print(f"DATA_DIR: {data_dir}")
    print(f"DB:       {db_path}")
    print(f"archive:  {archive_root}")
    print(f"mode:     {'APPLY (destructive)' if args.apply else 'dry-run'}")
    print()

    referenced = _collect_referenced_paths(db_path)
    print(f"Referenced paths in DB: {len(referenced)}")
    owned_dirs = _owned_archive_dirs(db_path, data_dir)
    print(f"Folders owned by archive rows: {len(owned_dirs)}")

    orphans: list[Path] = []
    total_files = 0
    total_bytes = 0
    # Library files are referenced by the same two tables and, since m177, live
    # under their own root - walk it against the same reference set.
    for walk_root in (archive_root, library_root):
        for rel, abs_path in _walk_archive_files(walk_root, data_dir, skip_dirs):
            total_files += 1
            if rel not in referenced and not any(_is_under(abs_path, d) for d in owned_dirs):
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    size = 0
                total_bytes += size
                orphans.append(abs_path)

    print(f"Total files scanned: {total_files}")
    print(f"Orphan files: {len(orphans)} ({total_bytes / 1_048_576:.1f} MiB)")
    print()

    for path in orphans:
        if args.apply:
            try:
                path.unlink()
                print(f"  deleted: {path}")
            except OSError as e:
                print(f"  error deleting {path}: {e}", file=sys.stderr)
        else:
            print(f"  would delete: {path}")

    print()
    print("Collapsing empty directories...")
    for walk_root in (archive_root, library_root):
        _collapse_empty_dirs(walk_root, skip_dirs, dry_run=not args.apply)

    print()
    orphan_dirs = _orphan_entity_dirs(db_path, data_dir)
    print(f"Orphan attachment directories: {len(orphan_dirs)}")
    for directory in orphan_dirs:
        if args.apply:
            try:
                shutil.rmtree(directory)
                print(f"  deleted: {directory}")
            except OSError as e:
                print(f"  error deleting {directory}: {e}", file=sys.stderr)
        else:
            print(f"  would delete: {directory}")

    if not args.apply and (orphans or orphan_dirs):
        print()
        print("(dry-run) re-run with --apply to actually delete the orphans above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
