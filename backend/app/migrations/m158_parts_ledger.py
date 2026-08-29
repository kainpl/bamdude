"""m158: parts ledger — per-part plate state + project part targets, plus
plan rows becoming per-plate. Three parts, all in the same unreleased
cycle (m158 hasn't shipped yet, so this is an amendment, not a new head):

1. ``print_archive_parts`` — the live per-part state of one printed plate
   (seeded at print start; skips and the defect dialog write into it).
2. ``project_parts`` — the project-wide target ledger keyed by canonical
   part name.
3. ``project_print_plan_items.plate_index`` — a plan row now names one
   plate of a file (0 = the whole file: single-plate files, raw gcode;
   1..N = that plate of a multi-plate 3MF), widening the unique
   constraint to ``(project_id, library_file_id, plate_index)``.

Design: docs/superpowers/specs/2026-08-29-project-parts-ledger-design.md.

Fresh installs get everything from ``create_all()``; every statement is
guarded. FK CASCADE is honoured by PostgreSQL only — this codebase never
sets ``PRAGMA foreign_keys`` on SQLite; hard-delete paths clean up
explicitly.

``seed()`` does two independent jobs, both upgrade-install-only (a fresh
install has nothing to expand or backfill) and both idempotent (``DEBUG=
true`` re-runs the head migration on every startup):

* Expand legacy multi-plate plan rows — a ``plate_index=0`` row whose
  file's metadata reports >1 plate becomes one row per plate, each
  inheriting ``copies``/``order_index`` (today's ``copies`` means "N x
  the whole file", and totals multiply whole-file metadata by copies, so
  per-plate inheritance keeps every sum identical).
* Backfill ``print_archive_parts`` for every pre-existing archive with a
  3MF still on disk. Users upgrade through migrations only, so the
  one-time population has to happen here; ``scripts/
  backfill_archive_parts.py`` remains the manual RE-RUN tool (rule
  changes, troubleshooting), not the normal upgrade path.
"""

import json
import logging
from pathlib import Path

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import column_exists, recreate_table, table_exists

logger = logging.getLogger(__name__)

version = 158
name = "parts_ledger"


# DDL for the widened plan table. SQLite-only literal (AUTOINCREMENT,
# DATETIME) — this only ever runs through recreate_table's SQLite branch;
# PostgreSQL gets the column + constraint via explicit ALTER TABLE below,
# since recreate_table's PostgreSQL branch only DROPs columns absent from
# the keep-list, it never ADDs one.
_PLAN_NEW_DDL = """
CREATE TABLE project_print_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    plate_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_project_file_plate UNIQUE (project_id, library_file_id, plate_index)
)
"""
# plate_index is deliberately absent — the OLD table doesn't have it, and
# recreate_table's SQLite branch only copies columns the source actually has.
_PLAN_KEEP_COLS = "id, project_id, library_file_id, copies, order_index, created_at, updated_at"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    json_t = "TEXT" if sqlite else "JSON"

    if not await table_exists(conn, "print_archive_parts"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE print_archive_parts (
                id {pk},
                archive_id INTEGER NOT NULL REFERENCES print_archives(id) ON DELETE CASCADE,
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                identify_ids {json_t},
                quantity INTEGER NOT NULL DEFAULT 1,
                defective INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_print_archive_parts_archive_id ON print_archive_parts (archive_id)")
        await conn.exec_driver_sql("CREATE INDEX ix_print_archive_parts_name_key ON print_archive_parts (name_key)")

    if not await table_exists(conn, "project_parts"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE project_parts (
                id {pk},
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                target_qty INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_project_parts_key UNIQUE (project_id, name_key)
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_project_parts_project_id ON project_parts (project_id)")

    if not sqlite:
        # m044 (frozen, released) reshapes this table's unique constraint
        # under a guard — `_has_constraint_or_index(conn, "uq_plan_project_
        # file")` — written before plate rows existed, i.e. before this
        # migration renamed the target to `uq_plan_project_file_plate`. On a
        # FRESH PostgreSQL install, create_all() already builds the table
        # with only `uq_plan_project_file_plate`, so m044's guard reads
        # false-for-"already has it" and its ADD CONSTRAINT branch fires
        # anyway, planting the old `uq_plan_project_file (project_id,
        # library_file_id)` unique back onto an otherwise-final table. The
        # `column_exists(..., "plate_index")` guard below is true on that
        # same fresh table (plate_index came from create_all() too), so the
        # ALTER-TABLE swap that would normally remove the old constraint
        # never runs. Drop it here, unconditionally and independent of the
        # plate_index guard, so every PostgreSQL install — fresh or
        # upgraded — ends with only the plate-scoped unique. IF EXISTS makes
        # this a no-op on upgrade paths where the swap below already
        # removed it, and on any re-run.
        await conn.exec_driver_sql(
            "ALTER TABLE project_print_plan_items DROP CONSTRAINT IF EXISTS uq_plan_project_file"
        )

    # Part 3 (added in the same unreleased cycle): plan rows become
    # per-plate. Existing DBs get the column + widened unique via table
    # recreate (SQLite) or ALTER TABLE (PostgreSQL); fresh installs already
    # have both from create_all() and skip this whole block.
    if not await column_exists(conn, "project_print_plan_items", "plate_index"):
        if sqlite:
            await recreate_table(conn, "project_print_plan_items", _PLAN_NEW_DDL, _PLAN_KEEP_COLS)
            # The recreate drops the table (and with it, any index tied to
            # its old identity) and builds a fresh one from _PLAN_NEW_DDL,
            # which carries no CREATE INDEX of its own — re-issue the one
            # m044 put on project_id, same as m044 does after its own
            # recreate of this table.
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_project_print_plan_items_project_id "
                    "ON project_print_plan_items(project_id)"
                )
            )
        else:
            await conn.exec_driver_sql(
                "ALTER TABLE project_print_plan_items ADD COLUMN IF NOT EXISTS plate_index INTEGER NOT NULL DEFAULT 0"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE project_print_plan_items DROP CONSTRAINT IF EXISTS uq_plan_project_file"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE project_print_plan_items ADD CONSTRAINT uq_plan_project_file_plate "
                "UNIQUE (project_id, library_file_id, plate_index)"
            )


async def seed(session_factory):
    """Two independent, unrelated jobs — see the module docstring."""

    # ---- job 1: expand legacy multi-plate plan rows into per-plate rows ----
    # Idempotent: a (project_id, library_file_id) pair that already has any
    # plate_index != 0 row is treated as already expanded and skipped; a
    # single-plate file's plate_index=0 row is left exactly as it is.
    # Named-column selects only — this seed must survive later schema drift.
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.id, p.project_id, p.library_file_id, p.copies, p.order_index, f.file_metadata "
                    "FROM project_print_plan_items p JOIN library_files f ON f.id = p.library_file_id "
                    "WHERE p.plate_index = 0"
                )
            )
        ).all()
        expanded = {
            (row[0], row[1])
            for row in (
                await session.execute(
                    text(
                        "SELECT DISTINCT project_id, library_file_id FROM project_print_plan_items "
                        "WHERE plate_index != 0"
                    )
                )
            ).all()
        }
        for row_id, project_id, file_id, copies, order_index, raw_meta in rows:
            if (project_id, file_id) in expanded:
                continue
            try:
                meta = raw_meta if isinstance(raw_meta, dict) else (json.loads(raw_meta) if raw_meta else {})
                plates = meta.get("plates") or []
                indices = sorted(
                    {int(p.get("index")) for p in plates if isinstance(p.get("index"), int) and p.get("index") > 0}
                )
                if len(indices) <= 1:
                    continue
                await session.execute(text("DELETE FROM project_print_plan_items WHERE id = :id"), {"id": row_id})
                for idx in indices:
                    await session.execute(
                        text(
                            "INSERT INTO project_print_plan_items "
                            "(project_id, library_file_id, copies, order_index, plate_index) "
                            "VALUES (:p, :f, :c, :o, :pl)"
                        ),
                        {"p": project_id, "f": file_id, "c": copies, "o": order_index, "pl": idx},
                    )
            except Exception:  # noqa: BLE001 — one corrupted file_metadata row must not abort the migration
                logger.warning("m158 seed: plan-row expansion skipped row %s", row_id, exc_info=True)
        await session.commit()

    # ---- job 2: one-time parts-ledger backfill for pre-existing archives ----
    # scripts/backfill_archive_parts.py stays as the manual RE-RUN tool
    # (rule changes, troubleshooting); first population happens here so
    # every user gets it on upgrade. Idempotent: archives that already have
    # rows are skipped, so a DEBUG re-run adds nothing. Path resolution and
    # error posture mirror m114_skip_objects_supported's precedent for a
    # migration that opens archive 3MFs.
    from backend.app.core.config import settings as _settings
    from backend.app.services.archive import extract_printable_objects_from_3mf
    from backend.app.services.part_names import tally_objects

    async with session_factory() as session:
        have_rows = {
            aid for (aid,) in (await session.execute(text("SELECT DISTINCT archive_id FROM print_archive_parts"))).all()
        }
        archives = (
            await session.execute(
                text(
                    "SELECT id, plate_index, file_path, defective_count FROM print_archives "
                    "WHERE file_path != '' AND deleted_at IS NULL"
                )
            )
        ).all()
        backfilled = 0
        for archive_id, plate_index, file_path, flat_defective in archives:
            if archive_id in have_rows:
                continue
            try:
                path = Path(file_path)
                if not path.is_absolute():
                    path = _settings.base_dir / file_path
                if not path.is_file():
                    continue
                objects = extract_printable_objects_from_3mf(path.read_bytes(), plate_number=plate_index)
                if not isinstance(objects, dict) or not objects:
                    continue
                tallies = tally_objects(objects)
                for part in tallies:
                    await session.execute(
                        text(
                            "INSERT INTO print_archive_parts "
                            "(archive_id, name, name_key, identify_ids, quantity, defective) "
                            "VALUES (:a, :n, :k, :ids, :q, 0)"
                        ),
                        {
                            "a": archive_id,
                            "n": part.name,
                            "k": part.name_key,
                            "ids": json.dumps(part.identify_ids),
                            "q": part.quantity,
                        },
                    )
                # Mono-plate rule (mirrors services/archive_parts.py::apply_flat_defective):
                # a plate holding copies of exactly ONE part adopts the legacy flat
                # count as that part's scrap. A multi-part plate stays unattributed —
                # there is no way to know which part went in the bin.
                if len(tallies) == 1 and (flat_defective or 0) > 0:
                    await session.execute(
                        text("UPDATE print_archive_parts SET defective = :d WHERE archive_id = :a"),
                        {"d": min(flat_defective, tallies[0].quantity), "a": archive_id},
                    )
                backfilled += 1
            except Exception:  # noqa: BLE001 — one bad 3MF must not sink the upgrade
                logger.warning("m158 seed: parts backfill skipped archive %s", archive_id, exc_info=True)
        if backfilled:
            logger.info("m158 seed: backfilled part rows for %d archive(s)", backfilled)
        await session.commit()
