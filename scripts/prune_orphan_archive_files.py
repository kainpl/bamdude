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

This script reconciles on-disk state against the DB. For every regular file
under ``<DATA_DIR>/archive/`` the relative path from ``DATA_DIR`` is checked
against the union of:

* ``print_archives.file_path`` + ``print_archives.thumbnail_path``
* ``library_files.file_path`` + ``library_files.thumbnail_path``

…across **both live and trashed** rows (``deleted_at`` IS NULL or NOT NULL):
trash-retained files still live on disk until the retention sweeper hard-
deletes them. Files that don't match any reference are orphans. After files
are removed, empty directories are collapsed bottom-up.

Skipped from the sweep:

* ``<DATA_DIR>/archive/temp/`` — runtime FTP staging area, rebuilt per upload
* anything outside ``<DATA_DIR>/archive/`` (the database's ``file_path`` is
  always relative to ``DATA_DIR``, but only ``archive/`` is BamDude's
  responsibility — leave certs, projects, virtual_printer alone).

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
import sqlite3
import sys
from pathlib import Path


def _resolve_data_dir(arg_data_dir: str | None) -> Path:
    if arg_data_dir:
        return Path(arg_data_dir).resolve()
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env).resolve()
    return (Path(__file__).resolve().parent.parent / "data").resolve()


def _collect_referenced_paths(db_path: Path) -> set[str]:
    """Build the set of relative paths (POSIX style, from DATA_DIR) the DB references.

    Includes trashed rows so files in the trash retention window aren't yanked
    from under the sweeper. POSIX-normalised because the DB stores forward-
    slash paths regardless of host OS, and on Windows `Path("a\\b").as_posix()`
    gives "a/b" — keeping comparison side-agnostic.
    """
    conn = sqlite3.connect(str(db_path))
    referenced: set[str] = set()
    for table, cols in (
        ("print_archives", ("file_path", "thumbnail_path")),
        ("library_files", ("file_path", "thumbnail_path")),
    ):
        cols_sql = ", ".join(cols)
        try:
            for row in conn.execute(f"SELECT {cols_sql} FROM {table}"):  # noqa: S608 — fixed columns
                for v in row:
                    if v:
                        referenced.add(Path(str(v)).as_posix())
        except sqlite3.OperationalError as e:
            print(f"warning: skipping {table}: {e}", file=sys.stderr)
    conn.close()
    return referenced


def _is_under(path: Path, ancestor: Path) -> bool:
    """True when ``path`` equals ``ancestor`` or is somewhere beneath it."""
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _walk_archive_files(archive_root: Path, data_dir: Path, skip_dirs: list[Path]):
    """Yield (relative_posix_path, abs_path) for regular files under archive/.

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
    skip_dirs = [archive_root / "temp"]

    if not db_path.exists():
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 1
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

    orphans: list[Path] = []
    total_files = 0
    total_bytes = 0
    for rel, abs_path in _walk_archive_files(archive_root, data_dir, skip_dirs):
        total_files += 1
        if rel not in referenced:
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
    _collapse_empty_dirs(archive_root, skip_dirs, dry_run=not args.apply)

    if not args.apply and orphans:
        print()
        print("(dry-run) re-run with --apply to actually delete the orphans above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
