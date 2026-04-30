#!/usr/bin/env python3
"""Rebuild a BamDude SQLite database with the canonical schema.

Creates a fresh DB by running the full BamDude init flow (``Base.metadata
.create_all`` + every migration in ``backend/app/migrations``), then copies
every row from the source DB into it table-by-table. Useful when the live
DB has accumulated structural drift through SQLite's limited ``ALTER TABLE``
or historical migrations that couldn't fully reproduce the canonical
``CREATE TABLE`` (column types, NOT NULL, CHECK constraints, defaults).

Default I/O:
  source = data/bamdude.db
  target = data/bamdude.db.new

After a successful rebuild:
  source -> source.bak.<timestamp>          (plus -wal / -shm sidecars)
  target -> source                          (data/bamdude.db is now rebuilt)

Usage:
  python scripts/normalize_db.py
  python scripts/normalize_db.py --source data/bamdude.db --target /tmp/new.db
  python scripts/normalize_db.py --no-rename            # leave both files in place

Refuses to start when target already exists, when source doesn't exist, or
when source is not a valid SQLite file. FK checks run after the copy and
violations are surfaced as warnings — the user asked for a verbatim data
copy, so the script doesn't refuse to finish on FK problems carried over
from the source. WAL is checkpointed and truncated before rename so the
on-disk file is self-contained.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Resolve project root early — we need it on sys.path before importing
# anything from ``backend.app``.
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"

# Tables we never copy verbatim:
# - ``_migrations`` — the fresh target already records every migration as
#   applied via ``run_all_migrations``; copying source's row set would lie
#   about which migrations actually ran on the new file.
# - ``archive_fts`` + its FTS5 shadow tables — auto-populated by triggers
#   that fire when we INSERT into ``print_archives``. Copying the binary
#   shadow content would race with the triggers.
# - ``sqlite_sequence`` — auto-managed by SQLite itself.
_SKIP_TABLES = frozenset(
    {
        "_migrations",
        "archive_fts",
        "archive_fts_data",
        "archive_fts_idx",
        "archive_fts_docsize",
        "archive_fts_config",
        "sqlite_sequence",
    }
)


def _is_sqlite_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rebuild a BamDude SQLite database with the canonical schema and copy data into it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_source = _DEFAULT_DATA_DIR / "bamdude.db"
    default_target = _DEFAULT_DATA_DIR / "bamdude.db.new"
    p.add_argument(
        "--source",
        type=Path,
        default=default_source,
        help=f"Path to the existing DB to read from (default: {default_source})",
    )
    p.add_argument(
        "--target",
        type=Path,
        default=default_target,
        help=f"Path the rebuilt DB will be written to (default: {default_target})",
    )
    p.add_argument(
        "--no-rename",
        action="store_true",
        help="Skip the final rename step (source stays put, target keeps its temp name)",
    )
    return p.parse_args()


def _die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"normalize_db: {msg}\n")
    sys.exit(code)


async def _init_target_schema() -> None:
    """Run the BamDude init flow against the target file, then re-create the
    ORM tables so the column order matches the current Python model exactly.

    ``DATABASE_URL`` env var must already point at the target file before
    this function is called — config.py reads it at module import time.

    Why the second pass: some migrations (notably m002) rebuild a table from
    a frozen DDL snapshot. Columns that later migrations re-add via
    ``ALTER TABLE ADD COLUMN`` end up appended at the end of the table
    rather than at the position the current model declares — for example,
    ``library_files.{print_count, deleted_at, source_type, source_url}``
    are model-declared between ``file_metadata`` and ``created_by_id`` but
    land at the very end of the table after migrations finish.

    Fix: after ``init_db`` populates ``_migrations`` with every applied
    version, drop every ORM-defined table and re-create from current model
    metadata. ``_migrations`` itself is created by raw SQL in the migration
    runner, so it isn't in ``Base.metadata`` and survives ``drop_all`` —
    a fresh BamDude startup against the rebuilt file sees every migration
    already applied and skips the upgrade phase. FTS5 triggers attached to
    ``print_archives`` are dropped along with the table; we explicitly
    re-attach them via m001's ``_setup_sqlite_fts``. The ``archive_fts``
    virtual table itself is not in ``Base.metadata`` and persists across
    the drop pass.
    """
    # Imported here so the env var override above takes effect first.
    from sqlalchemy import text

    from backend.app.core.database import Base, engine, init_db
    from backend.app.migrations.m001_bamdude_baseline import _setup_sqlite_fts

    await init_db()

    async with engine.begin() as conn:
        # FK off so DROP order across the dependency graph can't surface a
        # constraint error mid-rebuild. Required even though the connect
        # event doesn't enable FKs by default — defensive against future
        # changes to ``_set_sqlite_pragmas``.
        await conn.execute(text("PRAGMA foreign_keys = OFF"))
        # FTS triggers fire AFTER INSERT/UPDATE/DELETE on print_archives;
        # dropping print_archives auto-drops them, but explicit pre-drop
        # keeps the SQLAlchemy DROP visitor from tripping over them.
        for trigger in ("archive_fts_insert", "archive_fts_delete", "archive_fts_update"):
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await _setup_sqlite_fts(conn)

    await engine.dispose()


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _table_names(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _row_count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM {schema}."{table}"').fetchone()[0]


def _copy_data(source: Path, target: Path) -> tuple[list[tuple[str, int, int]], list[str], list[str]]:
    """Copy rows from source into target, schema by schema.

    Returns (copied, skipped_unknown, fk_violations_summary).
    ``copied`` is a list of (table_name, src_count, copied_count) tuples.
    ``skipped_unknown`` lists source tables absent from the target schema.
    """
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"ATTACH DATABASE '{source}' AS src")

    src_tables = sorted(_table_names(conn, "src"))
    tgt_tables = _table_names(conn, "main")

    copied: list[tuple[str, int, int]] = []
    skipped_unknown: list[str] = []

    # Copy in deterministic order. FKs are off so order doesn't matter for
    # constraint correctness; we sort alphabetically so the log is stable.
    for tbl in src_tables:
        if tbl in _SKIP_TABLES or tbl.startswith("archive_fts"):
            continue
        if tbl not in tgt_tables:
            skipped_unknown.append(tbl)
            continue

        src_cols = _columns(conn, "src", tbl)
        tgt_cols = set(_columns(conn, "main", tbl))
        common = [c for c in src_cols if c in tgt_cols]
        if not common:
            skipped_unknown.append(f"{tbl} (no common columns)")
            continue

        src_count = _row_count(conn, "src", tbl)
        cols_q = ", ".join(f'"{c}"' for c in common)

        # Wipe whatever the init flow seeded (e.g. ``color_catalog`` rows
        # from m001) so the row set after copy mirrors source verbatim.
        conn.execute(f'DELETE FROM main."{tbl}"')
        cur = conn.execute(f'INSERT INTO main."{tbl}" ({cols_q}) SELECT {cols_q} FROM src."{tbl}"')
        copied.append((tbl, src_count, cur.rowcount))

    conn.commit()

    # Surface FK violations as a warning rather than a fatal — verbatim copy
    # was the point. The user can fix data integrity separately.
    fk_violations = list(conn.execute("PRAGMA foreign_key_check"))
    fk_summary: list[str] = []
    if fk_violations:
        for v in fk_violations[:25]:
            # (table, rowid, parent, fk_id)
            fk_summary.append(f"  {v[0]} rowid={v[1]} -> {v[2]} (fk_id={v[3]})")
        if len(fk_violations) > 25:
            fk_summary.append(f"  ... +{len(fk_violations) - 25} more")

    # Re-enable FKs and checkpoint WAL so the on-disk file is self-contained
    # before we rename it.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass  # non-WAL or already truncated
    conn.execute("DETACH DATABASE src")
    conn.close()

    return copied, skipped_unknown, fk_summary


def _verify(source: Path, target: Path, copied: list[tuple[str, int, int]]) -> list[str]:
    """Sanity-check row counts: target table should equal source for every
    table we copied. Returns a list of mismatch descriptions (empty = OK)."""
    conn = sqlite3.connect(str(target))
    conn.execute(f"ATTACH DATABASE '{source}' AS src")
    mismatches: list[str] = []
    for tbl, src_count, _ in copied:
        try:
            tgt_count = _row_count(conn, "main", tbl)
        except sqlite3.OperationalError as exc:  # noqa: PERF203
            mismatches.append(f"{tbl}: query failed: {exc}")
            continue
        if tgt_count != src_count:
            mismatches.append(f"{tbl}: source={src_count} target={tgt_count}")
    conn.execute("DETACH DATABASE src")
    conn.close()
    return mismatches


def _finalize(source: Path, target: Path) -> tuple[Path, Path]:
    """Rename source -> source.bak.<timestamp>, target -> source path.
    Move WAL/SHM sidecars next to source out of the way too."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = source.with_name(source.name + f".bak.{timestamp}")

    # Move WAL/SHM sidecars (if any) of the source out of the way first so
    # the post-rename target's freshly-created sidecars don't collide with
    # leftovers.
    for sfx in ("-wal", "-shm"):
        sidecar = source.parent / (source.name + sfx)
        if sidecar.exists():
            shutil.move(str(sidecar), str(bak.parent / (bak.name + sfx)))

    shutil.move(str(source), str(bak))
    shutil.move(str(target), str(source))
    return bak, source


def main() -> int:
    args = _parse_args()
    source: Path = args.source.resolve()
    target: Path = args.target.resolve()

    # Sanity-check before we touch anything.
    if not source.exists():
        _die(f"source DB does not exist: {source}")
    if not _is_sqlite_file(source):
        _die(f"source is not a valid SQLite file: {source}")
    if target.exists():
        _die(f"target already exists; refusing to overwrite: {target}")
    if target == source:
        _die("source and target must differ")

    target.parent.mkdir(parents=True, exist_ok=True)

    # Point BamDude at the target file before any backend.app.* import — the
    # config module reads DATABASE_URL once at import time. Sqlalchemy URL
    # for sqlite uses forward slashes regardless of OS.
    target_uri = target.as_posix()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{target_uri}"

    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

    print(f"Source: {source}")
    print(f"Target: {target}")
    print(f"DATABASE_URL = {os.environ['DATABASE_URL']}")
    print()

    # Step 1 — fresh schema on target.
    print("[1/4] Initialising fresh schema on target (create_all + migrations)...")
    t0 = time.monotonic()
    asyncio.run(_init_target_schema())
    print(f"      done in {time.monotonic() - t0:.1f}s")

    # Step 2 — copy data.
    print("[2/4] Copying data from source...")
    t0 = time.monotonic()
    copied, skipped_unknown, fk_summary = _copy_data(source, target)
    print(f"      done in {time.monotonic() - t0:.1f}s")
    print(f"      copied {len(copied)} tables, {sum(c for _, _, c in copied)} rows total")
    if skipped_unknown:
        print(f"      skipped {len(skipped_unknown)} table(s) absent from canonical schema:")
        for name in skipped_unknown:
            print(f"        - {name}")
    if fk_summary:
        print(f"      WARNING: {len(fk_summary)} FK violations carried over from source:")
        for line in fk_summary:
            print(line)

    # Step 3 — verify counts.
    print("[3/4] Verifying row counts...")
    mismatches = _verify(source, target, copied)
    if mismatches:
        print("      FAILED — row-count mismatches:")
        for line in mismatches:
            print(f"        - {line}")
        _die("aborting before rename so target can be inspected")
    print("      OK — all copied tables match source row counts")

    # Step 4 — rename, unless --no-rename.
    if args.no_rename:
        print("[4/4] --no-rename set; leaving files in place.")
        print(f"      source: {source}")
        print(f"      target: {target}")
        return 0

    print("[4/4] Renaming files...")
    bak, new_main = _finalize(source, target)
    print(f"      old DB -> {bak}")
    print(f"      new DB -> {new_main}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
