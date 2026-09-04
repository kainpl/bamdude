"""m158: projects become orders; products, customers, order lines appear.

Design: docs/superpowers/specs/2026-09-02-projects-redesign-design.md.

**Why this number.** This slot first held ``m158_parts_ledger`` — the
per-part plate state, a ``project_parts`` target ledger and a per-plate
widening of the plan table. The redesign that followed retires both of the
latter, so shipping them and then converting them away would have made two
migrations out of one change and left every fresh install building tables
it drops three statements later. v0.5.5 ends at m156, so m157–m162 had
never been released: on 2026-09-03 the user folded the conversion (then
``m162_products_and_orders``) back into m158 and the m162 file went away.
⚠️ **Do not repeat this trick on a released migration** — anyone who has
already recorded the version would silently skip the new content. It was
only safe here because nothing shipped.

The whole conversion happens in ``upgrade(conn)`` — one transaction (FK off on
SQLite), the m044 precedent — so a conversion that fails half-way rolls back
whole and the legacy tables are still there for the next attempt.

**Runner order** (``migrations/__init__.py::_run_pending``): ``upgrade(conn)``
runs inside one ``engine.begin()`` and is COMMITTED when it returns; ``seed()``
runs after that, outside it, on its own sessions; only then does
``_record_migration`` write the version. So a ``seed()`` that raises leaves the
DDL and the conversion already committed with no ``_migrations`` row, and the
next boot re-enters m158 from the top. That is safe here: every DDL statement
is guarded, and ``_convert_legacy``'s marker (``library_file_projects``) was
dropped by the committed half, so the second pass converts nothing and only the
backfill is retried — which is itself idempotent.

Order of work:

1. ``print_archive_parts`` — the live per-part state of one printed plate
   (seeded at print start; skips and the defect dialog write into it). It
   comes FIRST because ``seed()`` backfills into it;
2. create the new tables and columns (guarded; fresh installs already have
   them from ``create_all()``);
3. IF the legacy pivot ``library_file_projects`` still exists, convert every
   project: one product, its files/folders/plates/parts, BOM → purchased
   parts + procurement, one order line ``× 1``, archives + queue rows get the
   line, ``budget → price``, ``archived → completed``; templates become
   products only and their attachments are COPIED to the product directory
   (never moved — see ``_copy_template_attachments``);
4. drop the five legacy tables and the six legacy ``projects`` columns.

Because step 4 removes the marker step 3 keys on, a second pass over this
module finds nothing to convert and touches no data. (``DEBUG=true`` will not
produce that pass — the dev re-run deletes and re-applies only the HIGHEST
version, m161 today, so m158 is never re-entered that way. The pass that does
happen is the one after a failed ``seed()``, above.) Named columns everywhere.

**Three starting shapes**, all of which reach this migration:

* **(i) straight from 0.5.5** — the ordinary upgrade. ``project_print_plan_
  items`` has NO ``plate_index`` and there is no ``project_parts`` table, so
  a plan row still means "N × the whole file". Such a row is expanded per
  plate from the file's own metadata during the conversion (see
  ``_expanded_plan_rows``), which is what the retired parts-ledger seed used
  to do to the plan table itself.
* **(ii) after the unreleased parts-ledger m158** — ``plate_index`` and
  ``project_parts`` are both present; plan rows are read as they are and
  ``target_qty`` is honoured — **except a zero on a part that is on a plate**.
  The retired ``services/project_parts.py`` seed planted every discovered part
  with ``target_qty = 0``, so such a zero is almost always that default and
  never a decision, and honouring it would convert to ``qty_per_unit = 0`` —
  "don't measure", which counts in no need and no surplus. It is therefore
  derived like any unseen part (``auto=True``). A zero on a part that is on
  NO plate keeps its zero: nothing ever seeded it, so somebody typed it.
  ⚠️ **Such a database reaches this shape only if
  somebody deletes its ``_migrations`` row first.** The runner keys on the
  VERSION alone (the recorded ``name`` is cosmetic), so a database holding
  ``(158, 'parts_ledger')`` counts m158 as applied and SKIPS this module
  entirely: it would keep the legacy tables, gain no products, and look
  healthy while doing it. The manual step before such a boot is
  ``DELETE FROM _migrations WHERE version IN (158, 162)``. Verified 2026-09-03:
  the parts-ledger m158 never reached ``dev`` or the ``:dev`` image (it lived
  only on ``origin/feature/v0.5.6-fixes``), so the only databases that ever
  need this are the developer's own SQLite and the dev PostgreSQL — no user
  install can be in shape (ii), and none needs the manual delete.
* **(iii) already converted** — the new tables are there and the legacy ones
  are gone. ``_convert_legacy`` sees no ``library_file_projects`` and returns;
  every DDL statement is guarded, so the whole run is a no-op.

**The legacy targets are kept, not dropped** (amended 2026-09-03, after the
first conversion of the user's live database). A project sized its work in
three unrelated places and none of them survived the first cut: a product came
out reading "780 + 780 per unit" ordered ``× 1``, which is the same need said
in the least useful way. Three rules fix that, all documented at their code:

* **rule A** (``_kit_and_quantity``) — every part the plates yield carries a
  target > 0 → the product becomes a kit, ``N = gcd(targets)`` becomes the
  line quantity and each part keeps ``target / N``;
* **rule B** (same helper) — no part targets, but ``target_parts_count`` or
  ``target_count`` divides EXACTLY into one run of the plan → that quotient is
  the line quantity;
* **rule D** (``seed_history_only_products``) — a product that ends up with no
  printed parts at all, because its files were deleted years ago, takes them
  from its own completed archives.

⚠️ **Whenever one of those rules puts a number above 1 on the line, the
purchased (BOM) totals must come down by the same factor** — a BOM row counted
the whole project, and the order now multiplies it by the quantity
(``_per_unit_from_total``, rounding UP so a requirement is never
under-provisioned). ``project_procurement.quantity_acquired`` stays absolute.
Miss this and "780 brackets + 780 screws" asks for 608 400 screws and can never
complete a unit.

``seed(session_factory)`` therefore does three jobs, in this order and only in
this order. **First** the library object backfill
(``services/library_objects_backfill.py``) plus
``_seed_printed_parts_from_plates``: a 3MF that never got its
``printable_objects`` gives its product no printed parts, and the conversion
above has already read that empty metadata — so the objects are filled and the
parts derived from them here. It asks that sweep NOT to touch products
(``sync_products=False``): the service reconciles through
``sync_product_for_file``, which emits entity-wide ORM selects, and a migration
running mid-chain may not. **Then** the one-time ``print_archive_parts``
backfill for pre-existing archives: users upgrade through migrations only, so
that population has to happen here; ``scripts/backfill_archive_parts.py``
remains the manual RE-RUN tool (rule changes, troubleshooting), not the normal
upgrade path. It is idempotent — archives that already have rows are skipped, so
the retry after a failed run adds nothing to what the first pass wrote.
⚠️ **Then** rule D, which reads exactly the rows that backfill has just written
and must not claim a product the library half can already measure — reorder any
of the three and a product stays part-less, silently.

FK CASCADE is honoured by PostgreSQL only — this codebase never sets
``PRAGMA foreign_keys`` on SQLite; hard-delete paths clean up explicitly.
"""

import json
import logging
import shutil
from collections import Counter
from math import gcd
from pathlib import Path

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, column_exists, get_table_columns, recreate_table, table_exists
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_files import product_attachments_dir

logger = logging.getLogger(__name__)

version = 158
name = "products_and_orders"

PURCHASED_KEY_PREFIX = "purchased:"

# SQLite literal for the recreate (the PostgreSQL branch of recreate_table
# only DROPs columns absent from the keep-list, so the literal is never run
# there). Must mirror models/project.py — including the four NOT NULLs, which
# `create_all()` put on the source table long before this migration runs, so
# no existing row can fail the copy and a fresh install cannot end up stricter
# than an upgraded one.
_PROJECTS_NEW_DDL = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    description TEXT,
    color VARCHAR(20),
    status VARCHAR(20) NOT NULL,
    notes TEXT,
    attachments JSON,
    tags TEXT,
    due_date DATETIME,
    priority VARCHAR(20) NOT NULL,
    price FLOAT,
    url VARCHAR(2048),
    cover_image_filename VARCHAR(255),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
)
"""
_PROJECTS_KEEP_COLS = (
    "id, name, customer_id, description, color, status, notes, attachments, tags, due_date, priority, price, url, "
    "cover_image_filename, created_at, updated_at"
)


async def _create_archive_parts(conn) -> None:
    """Per-part state of one printed plate. First, because ``seed()`` fills it.

    Shape (i) has never seen this table; shapes (ii) and (iii) already have it,
    from the retired parts-ledger form of m158 or from ``create_all()``.
    """
    if await table_exists(conn, "print_archive_parts"):
        return
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    json_t = "TEXT" if sqlite else "JSON"
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


async def _create_tables(conn) -> None:
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    json_t = "TEXT" if sqlite else "JSON"
    bool_t = "INTEGER" if sqlite else "BOOLEAN"
    true_ = "1" if sqlite else "TRUE"
    false_ = "0" if sqlite else "FALSE"
    ts = "DATETIME DEFAULT CURRENT_TIMESTAMP" if sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

    if not await table_exists(conn, "customers"):
        await conn.exec_driver_sql(
            f"CREATE TABLE customers (id {pk}, name VARCHAR(255) NOT NULL, contact TEXT, notes TEXT, "
            f"created_at {ts}, updated_at {ts})"
        )
    if not await table_exists(conn, "products"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE products (
                id {pk},
                name VARCHAR(255) NOT NULL,
                description TEXT,
                notes TEXT,
                designer VARCHAR(255),
                license VARCHAR(255),
                source_url VARCHAR(2048),
                design_id VARCHAR(64),
                cover_image_filename VARCHAR(255),
                attachments {json_t},
                is_active {bool_t} NOT NULL DEFAULT {true_},
                created_at {ts},
                updated_at {ts}
            )
            """
        )
    if not await table_exists(conn, "product_parts"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE product_parts (
                id {pk},
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                kind VARCHAR(16) NOT NULL DEFAULT 'printed',
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                qty_per_unit INTEGER NOT NULL DEFAULT 1,
                aliases {json_t},
                auto {bool_t} NOT NULL DEFAULT {false_},
                unit_price FLOAT,
                sourcing_url VARCHAR(512),
                remarks TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_product_parts_key UNIQUE (product_id, name_key)
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_product_parts_product_id ON product_parts (product_id)")
    if not await table_exists(conn, "product_files"):
        await conn.exec_driver_sql(
            "CREATE TABLE product_files (product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE, "
            "library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE, "
            "PRIMARY KEY (product_id, library_file_id))"
        )
    if not await table_exists(conn, "product_folders"):
        await conn.exec_driver_sql(
            "CREATE TABLE product_folders (product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE, "
            "library_folder_id INTEGER NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE, "
            "PRIMARY KEY (product_id, library_folder_id))"
        )
    if not await table_exists(conn, "product_plates"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE product_plates (
                id {pk},
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
                plate_index INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_product_plates_file_plate UNIQUE (product_id, library_file_id, plate_index)
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_product_plates_product_id ON product_plates (product_id)")
        await conn.exec_driver_sql("CREATE INDEX ix_product_plates_library_file_id ON product_plates (library_file_id)")
    if not await table_exists(conn, "project_lines"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE project_lines (
                id {pk},
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL DEFAULT 1,
                material VARCHAR(50),
                color VARCHAR(64),
                note TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at {ts},
                updated_at {ts}
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_project_lines_project_id ON project_lines (project_id)")
        await conn.exec_driver_sql("CREATE INDEX ix_project_lines_product_id ON project_lines (product_id)")
    if not await table_exists(conn, "project_procurement"):
        await conn.exec_driver_sql(
            "CREATE TABLE project_procurement (project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
            "product_part_id INTEGER NOT NULL REFERENCES product_parts(id) ON DELETE CASCADE, "
            "quantity_acquired INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project_id, product_part_id))"
        )

    await add_column(conn, "projects", "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL")
    await add_column(conn, "projects", "price FLOAT")
    for table in ("print_archives", "print_queue", "auto_queue_items"):
        await add_column(conn, table, "project_line_id INTEGER REFERENCES project_lines(id) ON DELETE SET NULL")
        # Attribution reads all three by line; the models carry index=True so a
        # fresh install already has these, hence IF NOT EXISTS (both dialects).
        await conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_project_line_id ON {table} (project_line_id)"
        )


def _load_meta(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _plate_names(meta: dict, plate_index: int) -> list[str]:
    """Raw object names on a plate, one entry per INSTANCE.

    ``plates[].objects`` is name-deduplicated (clones collapse to one entry);
    ``plates[].printable_objects`` is keyed by identify_id and truthful. Plate 0
    means the whole file: every plate's instances together, or the top-level
    ``printable_objects`` for files without plate metadata."""
    plates = [p for p in (meta.get("plates") or []) if isinstance(p, dict)]
    if plate_index > 0:
        plates = [p for p in plates if p.get("index") == plate_index]
    names: list[str] = []
    for plate in plates:
        po = plate.get("printable_objects")
        if isinstance(po, dict) and po:
            names.extend(str(v) for v in po.values())
        else:
            names.extend(str(v) for v in (plate.get("objects") or []))
    if not names and plate_index == 0:
        po = meta.get("printable_objects")
        if isinstance(po, dict):
            names.extend(str(v) for v in po.values())
    return names


def _plate_key_counts(meta: dict, plate_index: int) -> tuple[Counter, dict[str, str]]:
    """name_key → instances on the plate, and name_key → display spelling."""
    raw = _plate_names(meta, plate_index)
    counts: Counter = Counter()
    display: dict[str, str] = {}
    for r in raw:
        canon = canonicalize(r, raw)
        key = name_key(canon)
        counts[key] += 1
        display.setdefault(key, canon)
    return counts, display


def _expanded_plan_rows(rows, has_plate_index: bool) -> list[tuple]:
    """Plan rows as ``(file_id, plate_index, copies, raw_meta)``, per plate.

    On shape (ii) the rows already name a plate and come through untouched. On
    shape (i) there is no ``plate_index`` column at all — a row means "N × the
    whole file" — so a file whose metadata reports more than one plate becomes
    one row PER plate, each inheriting ``copies``. That is exactly what the
    retired parts-ledger seed did to the plan table, moved here: totals used to
    multiply whole-file metadata by ``copies``, and per-plate inheritance keeps
    every sum identical. A single-plate file (or one with no plate metadata)
    stays a single ``plate_index = 0`` row, which ``_plate_names`` reads as "the
    whole file".

    Metadata this cannot read costs the row its expansion, not the upgrade: it
    is kept as a single whole-file plate and says so in the log. Nothing further
    downstream will report it — ``_load_meta`` hands ``_plate_key_counts`` an
    empty dict for the same bytes, which raises nothing and simply yields no
    parts, so this is the only place the loss is visible.
    """
    if has_plate_index:
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    expanded: list[tuple] = []
    for file_id, _zero, copies, raw_meta in rows:
        try:
            plates = _load_meta(raw_meta).get("plates") or []
            indices = sorted({int(p["index"]) for p in plates if isinstance(p.get("index"), int) and p["index"] > 0})
        except Exception:  # noqa: BLE001 — one unreadable file_metadata must not abort the upgrade
            logger.warning(
                "m158: plan row for file %s has unreadable metadata, kept as a whole-file plate",
                file_id,
                exc_info=True,
            )
            indices = []
        if len(indices) > 1:
            expanded.extend((file_id, idx, copies, raw_meta) for idx in indices)
        else:
            expanded.append((file_id, 0, copies, raw_meta))
    return expanded


async def _insert_returning_id(conn, sql: str, params: dict) -> int:
    """INSERT … RETURNING id — SQLite ≥ 3.35 (Python 3.12 bundles 3.4x) and PostgreSQL."""
    return (await conn.execute(text(sql + " RETURNING id"), params)).scalar_one()


def _per_unit_from_total(total: int, units: int, what: str) -> int:
    """A legacy PROJECT TOTAL restated per one unit — rounded UP, never down.

    Purchased rows are the only figures in the legacy schema that counted the
    whole project rather than one unit, so they are the only ones that have to
    be divided when rules A, B or D give the line a quantity above 1.

    Rounding is upwards on purpose: a remainder means the operator's total was
    never a whole number of units (they bought a 1000-pack for 780 kits), and
    under-provisioning a requirement is the one direction that can stop a build.
    The result is therefore never 0 either — a purchased part with
    ``qty_per_unit = 0`` would drop out of both the need and the progress.
    """
    if units <= 1:
        return total
    exact, remainder = divmod(total, units)
    if not remainder:
        return exact
    logger.info("m158: %s — %d does not divide by %d, rounded up to %d per unit", what, total, units, exact + 1)
    return exact + 1


def _kit_and_quantity(
    row: dict,
    targets: dict[str, tuple[str, int | None]],
    yield_by_key: Counter,
    plan_rows: list,
) -> tuple[int, int]:
    """``(kit_size, line_quantity)`` — rules A and B (spec §Migration 4 and 6).

    The legacy project carried its targets in three unrelated places and the
    first cut of this migration kept none of them: a project of "780 brackets
    + 780 lids" converted to a product of *780 + 780 per unit* ordered *× 1*,
    which is the same need said in the least useful way.

    **Rule A** — when every part the plates yield also carries a target > 0,
    the targets are a multiple of one kit: ``N = gcd(targets)`` comes out as
    the line quantity and each part keeps ``target / N``. The need per part is
    unchanged (``N × qty_per_unit == target``) but the product now reads as
    what it is — 1 + 1, ordered 780 times. Coprime targets give ``N = 1``,
    i.e. exactly the old behaviour, so nothing regresses.

    The condition is deliberately *every counted part*, not *any*: a part that
    is on a plate but was never given a target has no share in the kit, and
    inventing one would put a number nobody chose into the composition. Such a
    product falls through to rule B with its targets untouched.

    **Rule B** — no part targets to divide, but the project's own
    ``target_parts_count`` / ``target_count`` may still divide exactly into
    what one run of the plan produces. Exactly, or not at all: a remainder
    means the two numbers never described the same plan (the plan was edited
    after the target was typed), and rounding it would silently restate the
    operator's target. ``target_parts_count`` is tried first because it counts
    the same things the parts do; ``target_count`` counts whole plan runs, so
    it is divided by Σ plan copies instead.

    Neither rule can invent a target where there is none: a project with no
    targets at all converts to ``× 1`` and rule D may still fill it from
    history in ``seed()``.
    """
    counted = {key: target for key, (_name, target) in targets.items() if target and target > 0}
    if counted and set(yield_by_key) <= set(counted):
        n = gcd(*counted.values())
        return n, n

    if yield_by_key:
        per_run = sum(yield_by_key.values())
        plan_copies = sum((r[2] or 1) for r in plan_rows)
        target_parts_count = row.get("target_parts_count") or 0
        target_count = row.get("target_count") or 0
        if per_run > 0 and target_parts_count > 0 and target_parts_count % per_run == 0:
            return 1, target_parts_count // per_run
        if plan_copies > 0 and target_count > 0 and target_count % plan_copies == 0:
            return 1, target_count // plan_copies
    return 1, 1


async def _convert_one_project(conn, row: dict, *, is_template: bool) -> None:
    project_id = row["id"]
    product_id = await _insert_returning_id(
        conn,
        "INSERT INTO products (name, description, is_active, cover_image_filename, attachments) "
        "VALUES (:n, :d, :a, :c, :att)",
        {
            "n": row["name"],
            "d": row["description"],
            "a": 1 if is_sqlite() else True,
            "c": row["cover_image_filename"] if is_template else None,
            "att": _template_attachments_json(row) if is_template else None,
        },
    )

    # The EXISTS guards drop dangling pivot rows. SQLite never enforced these
    # FKs (this codebase does not set PRAGMA foreign_keys), so a legacy pivot
    # can point at a library row that is long gone; the new pivots DO carry
    # enforced FKs on the SQLite→PostgreSQL auto-migrate path, where copying
    # one such row would abort the whole migration.
    await conn.execute(
        text(
            "INSERT INTO product_files (product_id, library_file_id) "
            "SELECT :pid, file_id FROM library_file_projects WHERE project_id = :p "
            "AND EXISTS (SELECT 1 FROM library_files lf WHERE lf.id = library_file_projects.file_id)"
        ),
        {"pid": product_id, "p": project_id},
    )
    await conn.execute(
        text(
            "INSERT INTO product_folders (product_id, library_folder_id) "
            "SELECT :pid, folder_id FROM library_folder_projects WHERE project_id = :p "
            "AND EXISTS (SELECT 1 FROM library_folders lf WHERE lf.id = library_folder_projects.folder_id)"
        ),
        {"pid": product_id, "p": project_id},
    )

    # Shape (i) has no plate_index column; the literal 0 keeps one row shape for
    # both, and _expanded_plan_rows turns it into real plates from the metadata.
    has_plate_index = await column_exists(conn, "project_print_plan_items", "plate_index")
    plate_col = "p.plate_index" if has_plate_index else "0 AS plate_index"
    plan_rows = (
        await conn.execute(
            text(
                f"SELECT p.library_file_id, {plate_col}, p.copies, f.file_metadata "
                "FROM project_print_plan_items p JOIN library_files f ON f.id = p.library_file_id "
                "WHERE p.project_id = :p"
            ),
            {"p": project_id},
        )
    ).all()
    seen_plates: set[tuple[int, int]] = set()
    yield_by_key: Counter = Counter()  # Σ copies × instances
    first_count: dict[str, int] = {}
    display: dict[str, str] = {}
    from backend.app.services.library_objects_backfill import _objects_recorded

    #: (file, plate) → Σ copies, for the plates whose metadata cannot be read yet.
    #: ⚠️ Summed over EVERY plan row, exactly as ``yield_by_key`` below is: two
    #: rows for the same plate at 2 and 3 copies are five, not two.
    pending_copies: Counter = Counter()
    for file_id, plate_index, copies, raw_meta in _expanded_plan_rows(plan_rows, has_plate_index):
        if (file_id, plate_index) not in seen_plates:
            seen_plates.add((file_id, plate_index))
            await conn.execute(
                text("INSERT INTO product_plates (product_id, library_file_id, plate_index) VALUES (:pid, :f, :pl)"),
                {"pid": product_id, "f": file_id, "pl": plate_index},
            )
        # ⚠️ A file whose metadata has never been ASKED what it contains yields
        # nothing below, and ``seed()`` fills it a moment later — by which time
        # ``project_print_plan_items`` is dropped and ``copies`` is gone. Hand it
        # forward. Same predicate the backfill's worklist uses, imported rather
        # than restated so the two cannot disagree about what "answered" means.
        meta = _load_meta(raw_meta)  # decoded once: both readers below want the same dict
        if not _objects_recorded(meta):
            pending_copies[(file_id, plate_index)] += copies or 1
        try:
            counts, names = _plate_key_counts(meta, plate_index)
        except Exception:  # noqa: BLE001 — one corrupt file_metadata must not abort the upgrade
            logger.warning(
                "m158: skipped metadata of file %s while converting project %s", file_id, project_id, exc_info=True
            )
            continue
        for key, n in counts.items():
            yield_by_key[key] += (copies or 1) * n
            first_count.setdefault(key, n)
            display.setdefault(key, names[key])

    for (file_id, plate_index), copies in sorted(pending_copies.items()):
        await conn.execute(
            text(
                f"INSERT INTO {_PENDING_COPIES} (product_id, library_file_id, plate_index, copies) "
                "VALUES (:pid, :f, :pl, :c)"
            ),
            {"pid": product_id, "f": file_id, "pl": plate_index, "c": copies},
        )

    # Shape (i) never had a target ledger — every printed part is then derived.
    targets: dict[str, tuple[str, int | None]] = {}
    if await table_exists(conn, "project_parts"):
        targets = {
            r[1]: (r[0], r[2])
            for r in (
                await conn.execute(
                    text("SELECT name, name_key, target_qty FROM project_parts WHERE project_id = :p"),
                    {"p": project_id},
                )
            ).all()
        }
    kit_size, line_quantity = _kit_and_quantity(row, targets, yield_by_key, plan_rows)

    sort_order = 0
    for key in sorted(set(targets) | set(yield_by_key)):
        if key in targets:
            part_name, target = targets[key]
            if target and target > 0:
                # ``kit_size`` is 1 unless rule A fired, so this is the plain
                # "explicit target wins" rule in every other case.
                qty, auto = target // kit_size, False
            elif target == 0 and key not in yield_by_key:
                # An operator zero — "don't measure this one" — and the only
                # zero we can tell apart. The retired seed planted EVERY
                # discovered part with target_qty = 0, so a zero on a part that
                # IS on a plate is far more likely to be that default than a
                # decision, and carrying it across would make the part
                # unmeasurable: qty_per_unit = 0 counts in no need and no
                # surplus. A part on no plate was never seeded by that walk, so
                # its zero can only have been typed.
                qty, auto = 0, False
            else:
                qty, auto = (yield_by_key.get(key) or first_count.get(key) or 1), True
        else:
            part_name = display[key]
            qty, auto = (yield_by_key.get(key) or first_count.get(key) or 1), True
        await conn.execute(
            text(
                "INSERT INTO product_parts (product_id, kind, name, name_key, qty_per_unit, aliases, auto, sort_order) "
                "VALUES (:pid, 'printed', :n, :k, :q, :al, :auto, :so)"
            ),
            {
                "pid": product_id,
                "n": part_name,
                "k": key,
                "q": qty,
                "al": json.dumps([key]),
                "auto": (1 if auto else 0) if is_sqlite() else auto,
                "so": sort_order,
            },
        )
        sort_order += 1

    bom_rows = (
        await conn.execute(
            text(
                "SELECT name, quantity_needed, quantity_acquired, unit_price, sourcing_url, remarks "
                "FROM project_bom_items WHERE project_id = :p ORDER BY sort_order, id"
            ),
            {"p": project_id},
        )
    ).all()
    line_id: int | None = None
    if not is_template:
        line_id = await _insert_returning_id(
            conn,
            "INSERT INTO project_lines (project_id, product_id, quantity, sort_order) VALUES (:p, :pid, :q, 0)",
            {"p": project_id, "pid": product_id, "q": line_quantity},
        )
    # A BOM row counted the WHOLE project ("780 screws"), which was the same
    # number as a per-unit figure only while the line was the literal × 1.
    # ``order_metrics`` needs `qty_per_unit × quantity`, so the total has to be
    # divided by however many units the line now orders — or the same rule that
    # made the product readable would turn 780 screws into 608 400 of them and
    # cap the order at one completed unit forever (``_units_complete`` divides
    # the acquired total by ``qty_per_unit``). A template has no line, so its
    # totals cover ``kit_size`` units instead — which is 1 unless rule A fired,
    # keeping the divisor paired with what the printed parts did.
    bom_divisor = kit_size if is_template else line_quantity
    used_keys: set[str] = set()
    for bom_name, needed, acquired, price, url, remarks in bom_rows:
        key = PURCHASED_KEY_PREFIX + " ".join((bom_name or "").split()).lower()
        if key in used_keys:  # two BOM rows with the same name — keep the first
            continue
        used_keys.add(key)
        per_unit = _per_unit_from_total(needed or 1, bom_divisor, f"BOM row {bom_name!r} of project {project_id}")
        part_id = await _insert_returning_id(
            conn,
            "INSERT INTO product_parts "
            "(product_id, kind, name, name_key, qty_per_unit, unit_price, sourcing_url, remarks, auto, sort_order) "
            "VALUES (:pid, 'purchased', :n, :k, :q, :price, :url, :rem, :auto, :so)",
            {
                "pid": product_id,
                "n": bom_name,
                "k": key,
                "q": per_unit,
                "price": price,
                "url": url,
                "rem": remarks,
                "auto": 0 if is_sqlite() else False,
                "so": sort_order,
            },
        )
        sort_order += 1
        if line_id is not None:
            await conn.execute(
                text(
                    "INSERT INTO project_procurement (project_id, product_part_id, quantity_acquired) "
                    "VALUES (:p, :part, :q)"
                ),
                {"p": project_id, "part": part_id, "q": acquired or 0},
            )

    if is_template:
        _copy_template_attachments(project_id, product_id)
        await conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
        return

    for table in ("print_archives", "print_queue", "auto_queue_items"):
        await conn.execute(
            text(f"UPDATE {table} SET project_line_id = :l WHERE project_id = :p"), {"l": line_id, "p": project_id}
        )


def _template_attachments_json(row: dict) -> str | None:
    """Legacy attachment entries gain the typed shape (category 'other')."""
    raw = _load_meta(row.get("attachments"))
    entries = raw if isinstance(raw, list) else []
    converted = [
        {
            "category": "other",
            "filename": e.get("filename"),
            "original_name": e.get("original_name") or e.get("filename"),
            "size": e.get("size"),
            "sort_order": i,
            "source": "manual",
            "uploaded_at": e.get("uploaded_at"),
        }
        for i, e in enumerate(entries)
        if isinstance(e, dict) and e.get("filename")
    ]
    return json.dumps(converted) if converted else None


def _copy_template_attachments(project_id: int, product_id: int) -> None:
    """COPY, never move — this runs inside a transaction that can still roll back.

    Moving would destroy the source before anything committed: a later raise
    rolls the DB back, but the files are already gone, the retry finds no
    source directory and the new product ends up naming files stranded under a
    product id that never existed. Copying leaves
    ``projects/<id>/attachments`` untouched, so a retry simply copies again
    into the new id and the only residue of a failed attempt is an orphan
    ``products/<old id>/`` directory that nothing reads.
    """
    from backend.app.core.config import settings

    src = Path(settings.archive_dir) / "projects" / str(project_id) / "attachments"
    if not src.is_dir():
        return
    dst = product_attachments_dir(product_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


async def _convert_legacy(conn) -> None:
    if not await table_exists(conn, "library_file_projects") or not await column_exists(
        conn, "projects", "is_template"
    ):
        return  # fresh install, or already converted — nothing to do

    has_budget = await column_exists(conn, "projects", "budget")
    has_cover = await column_exists(conn, "projects", "cover_image_filename")
    cols = "id, name, description, status, is_template, attachments"
    cols += ", budget" if has_budget else ", NULL AS budget"
    cols += ", cover_image_filename" if has_cover else ", NULL AS cover_image_filename"
    # Rule B reads these two; ``_drop_legacy`` removes them three statements
    # later, which is why nothing downstream of ``upgrade`` can ask for them.
    for legacy in ("target_count", "target_parts_count"):
        cols += f", {legacy}" if await column_exists(conn, "projects", legacy) else f", NULL AS {legacy}"
    rows = (await conn.execute(text(f"SELECT {cols} FROM projects ORDER BY id"))).mappings().all()

    converted = 0
    for row in rows:
        await _convert_one_project(conn, dict(row), is_template=bool(row["is_template"]))
        converted += 1
    if has_budget:
        await conn.execute(text("UPDATE projects SET price = budget WHERE budget IS NOT NULL AND price IS NULL"))
    await conn.execute(text("UPDATE projects SET status = 'completed' WHERE status = 'archived'"))
    logger.info("m158: converted %d project(s) into products + order lines", converted)


async def _drop_legacy(conn) -> None:
    for table in (
        "project_bom_items",
        "project_print_plan_items",
        "project_parts",
        "library_file_projects",
        "library_folder_projects",
    ):
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
    present = set(await get_table_columns(conn, "projects"))
    if present & {"target_count", "target_parts_count", "parent_id", "is_template", "template_source_id", "budget"}:
        await recreate_table(conn, "projects", _PROJECTS_NEW_DDL, _PROJECTS_KEEP_COLS)


#: Scratch table, created in ``upgrade()`` and dropped at the end of ``seed()``.
#:
#: ⚠️ It exists because ``copies`` is UNRECOVERABLE by the time ``seed()`` runs:
#: ``_drop_legacy`` removes ``project_print_plan_items`` inside the same
#: ``upgrade()`` transaction that read it. A converted part's ``qty_per_unit`` is
#: ``Σ(copies × instances)`` — so a plan row of ``copies = 2`` whose file's
#: metadata was empty at conversion time would have its per-unit need understated
#: by exactly that factor when ``seed()`` derives the part later. This carries
#: the factor across, one row per plate the conversion could not measure.
_PENDING_COPIES = "_m158_pending_plate_copies"


async def _create_pending_plate_copies(conn) -> None:
    await conn.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {_PENDING_COPIES} ("
        "product_id INTEGER NOT NULL, library_file_id INTEGER NOT NULL, "
        "plate_index INTEGER NOT NULL, copies INTEGER NOT NULL DEFAULT 1)"
    )


async def upgrade(conn):
    await _create_archive_parts(conn)
    await _create_tables(conn)
    await _create_pending_plate_copies(conn)
    await _convert_legacy(conn)
    await _drop_legacy(conn)


async def _pending_plate_copies(session) -> dict[tuple[int, int, int], int]:
    """``(product, file, plate) → Σ copies`` handed over by the conversion.

    Empty when the table is gone — which is the normal state on every run after
    the first, and on a ``seed()`` re-entry after the drop. Missing means "no
    pending copies", never an error: the factor is a correction to apply when it
    is there, not a precondition for seeding parts at all.
    """
    if not await table_exists(session, _PENDING_COPIES):
        return {}
    rows = (
        await session.execute(text(f"SELECT product_id, library_file_id, plate_index, copies FROM {_PENDING_COPIES}"))
    ).all()
    return {(int(p), int(f), int(pl)): int(c or 1) for p, f, pl, c in rows}


async def _seed_printed_parts_from_plates(session, library_file_ids: list[int]) -> int:
    """Every plate of these FILES yields printed parts its products do not have.

    The text-SQL twin of ``product_sync.seed_parts_for_product``, run once in
    ``seed()`` after the library object backfill has filled the metadata this
    reads. Returns the number of parts created.

    ⚠️ **Scoped to the files the backfill just filled**, never to every product
    plate in the database. A hand re-run that walked them all would re-create an
    ``auto`` part an operator had deleted, on a product this run never touched —
    a migration silently editing a composition somebody curated.

    ⚠️ **Why not ``sync_product_for_file``**, which is the single writer of this
    everywhere else: it emits ``select(ProductPlate)`` and ``select(ProductPart)``
    — entity-wide ORM selects against TODAY's models. A migration runs mid-chain,
    so the day a later migration adds a column to either table this seed would
    emit SQL the database does not have yet, for every user upgrading from an
    older release. Named columns only, here as everywhere in this file.

    ⚠️ **``qty_per_unit`` is ``Σ copies × instances``, not instances.** That is
    what ``_convert_one_project`` writes for a derived part (``yield_by_key``),
    and what rule B divides its targets by — a part seeded here at the bare
    instance count would understate the per-unit need of every converted project
    whose plan asked for more than one copy. The factor comes from
    ``_PENDING_COPIES``; a plate nobody recorded is one copy, which is what a
    file linked to a product directly has always meant.

    ⚠️ **Only ADDS.** A key already covered by a part's ``name_key`` or by one of
    its aliases is left alone, so an operator's edits survive and a re-run writes
    nothing. A product with no plate rows is untouched — a plate is what says
    "this file makes parts for this product", and rule D handles the rest.
    """
    if not library_file_ids:
        return 0
    copies_by_plate = await _pending_plate_copies(session)
    # ⚠️ Sliced into batches: a library of thousands of freshly filled files would
    # otherwise put every id into ONE ``IN (...)`` list, which SQLite refuses past
    # its host-parameter/expression limits and PostgreSQL merely plans badly. 500
    # is the same batch size the library scanner writes in (m148).
    rows: list = []
    for start in range(0, len(library_file_ids), 500):
        # Server-side ints, straight from ``library_files.id`` — never request input.
        ids_sql = ",".join(str(int(i)) for i in library_file_ids[start : start + 500])
        rows += (
            await session.execute(
                text(
                    "SELECT pp.product_id, pp.library_file_id, pp.plate_index, lf.file_metadata "
                    "FROM product_plates pp JOIN library_files lf ON lf.id = pp.library_file_id "
                    f"WHERE pp.library_file_id IN ({ids_sql}) AND lf.deleted_at IS NULL "
                    "ORDER BY pp.product_id, pp.library_file_id, pp.plate_index"
                )
            )
        ).all()
    # Each batch is ordered; the concatenation of two is not. Sorted here so the
    # ``sort_order`` a part is given does not depend on how the ids were sliced.
    rows.sort(key=lambda r: (r[0], r[1], r[2] or 0))
    by_product: dict[int, list[tuple[int, int, dict]]] = {}
    for product_id, file_id, plate_index, raw_meta in rows:
        try:
            meta = _load_meta(raw_meta)
        except Exception:  # noqa: BLE001 — one unreadable file_metadata must not abort the upgrade
            logger.warning("m158 seed: unreadable metadata on a plate of product %s", product_id, exc_info=True)
            continue
        by_product.setdefault(product_id, []).append((file_id, plate_index or 0, meta))

    created = 0
    for product_id, plates in by_product.items():
        existing = (
            await session.execute(
                text("SELECT name_key, aliases, sort_order FROM product_parts WHERE product_id = :p"),
                {"p": product_id},
            )
        ).all()
        known: set[str] = set()
        next_sort = -1
        for part_key, raw_aliases, sort_order in existing:
            known.add(part_key)
            aliases = raw_aliases
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except (TypeError, ValueError):
                    aliases = []
            for alias in aliases or []:
                known.add(str(alias))
            next_sort = max(next_sort, int(sort_order or 0))
        next_sort += 1

        for file_id, plate_index, meta in plates:
            try:
                counts, display = _plate_key_counts(meta, plate_index)
            except Exception:  # noqa: BLE001 — one corrupt file_metadata must not abort the upgrade
                logger.warning("m158 seed: skipped plate metadata of product %s", product_id, exc_info=True)
                continue
            copies = copies_by_plate.get((product_id, file_id, plate_index), 1)
            for key, instances in counts.items():
                if key in known:
                    continue
                await session.execute(
                    text(
                        "INSERT INTO product_parts "
                        "(product_id, kind, name, name_key, qty_per_unit, aliases, auto, sort_order) "
                        "VALUES (:pid, 'printed', :n, :k, :q, :al, :auto, :so)"
                    ),
                    {
                        "pid": product_id,
                        "n": display[key],
                        "k": key,
                        "q": instances * copies,
                        "al": json.dumps([key]),
                        "auto": 1 if is_sqlite() else True,
                        "so": next_sort,
                    },
                )
                # ⚠️ Marked known before the flush: within one product a key
                # belongs to exactly one part, and two plates carrying the same
                # object would otherwise each insert one and collide on
                # ``uq_product_parts_key``.
                known.add(key)
                next_sort += 1
                created += 1
    return created


async def seed(session_factory):
    """Library object metadata, then the parts-ledger backfill, then rule D.

    The archive half: ``scripts/backfill_archive_parts.py`` stays as the manual
    RE-RUN tool (rule changes, troubleshooting); first population happens here so
    every user gets it on upgrade. Idempotent: archives that already have rows
    are skipped, so a ``DEBUG=true`` re-run adds nothing. Path resolution and
    error posture mirror m114_skip_objects_supported's precedent for a migration
    that opens archive 3MFs. Named-column selects only — this seed must survive
    later schema drift, which is also why the library half below seeds its parts
    through ``_seed_printed_parts_from_plates`` rather than the ORM service.
    """
    from backend.app.core.config import settings as _settings
    from backend.app.services.archive import extract_printable_objects_from_3mf
    from backend.app.services.library_objects_backfill import backfill_library_objects
    from backend.app.services.part_names import tally_objects

    # ── First: the LIBRARY half of the same hole ────────────────────────────
    # A 3MF whose ``file_metadata`` never got ``printable_objects`` — uploaded
    # before the extractor existed, or scanned while its mount was down — gives
    # its product no printed parts and the plan nothing to count. It runs FIRST
    # because everything after it reads ``file_metadata``: the parts seeded from
    # it immediately below, and rule D at the end, which must not treat a product
    # we can now measure from its own file as history-only.
    #
    # ⚠️ ``sync_products=False``. The sweep's own reconciliation goes through
    # ``sync_product_for_file``, which emits entity-wide ORM selects — a
    # migration running mid-chain may not. ``_seed_printed_parts_from_plates``
    # does that job in named-column text SQL instead. The boot sweep in
    # ``main.py`` keeps the ORM path for the files this upgrade could not reach.
    #
    # ⚠️ The conversion in ``upgrade()`` has already run and committed by now, so
    # it read the EMPTY metadata; the parts it could not derive are derived here
    # instead. Filling the objects before the conversion is not available to us —
    # ``upgrade`` is one transaction, and unzipping a library inside it is the
    # very lock-holding this codebase forbids (m148).
    library_objects = await backfill_library_objects(session_factory, sync_products=False)
    if library_objects.filled or library_objects.skipped_unreachable or library_objects.skipped_unparseable:
        logger.info(
            "m158 seed: object metadata filled for %d of %d library 3MF(s) (%d unreachable, %d unparseable)",
            library_objects.filled,
            library_objects.scanned,
            library_objects.skipped_unreachable,
            library_objects.skipped_unparseable,
        )
    # ⚠️ Scoped to the ids THIS run filled PLUS the files the conversion could not
    # measure, not to "something was filled". A re-run must not walk the plates of
    # a product it did not touch and put back an ``auto`` part somebody deleted on
    # purpose — and every row of ``_PENDING_COPIES`` is by construction a plate
    # this migration owes parts to, so the union widens the walk to those and to
    # no curated product.
    #
    # ⚠️ The union is what makes ``seed()`` RE-ENTRANT. ``library_objects_backfill
    # ._write_chunk`` commits per chunk, so a ``seed()`` interrupted after the
    # chunks landed and before this step comes back to files that are already
    # filled and therefore to an EMPTY ``filled_ids``: gating on that alone
    # skipped the parts step and then dropped the hand-over at the bottom of this
    # function, losing the ``copies`` factor with nothing left to recover it from.
    async with session_factory() as session:
        pending_files = {file_id for _product_id, file_id, _plate in await _pending_plate_copies(session)}
        parts_scope = sorted(set(library_objects.filled_ids) | pending_files)
        if parts_scope:
            seeded = await _seed_printed_parts_from_plates(session, parts_scope)
            await session.commit()
            if seeded:
                logger.info("m158 seed: %d printed part(s) derived from the newly filled objects", seeded)

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
                plate_objects = extract_printable_objects_from_3mf(path.read_bytes(), plate_number=plate_index)
                if not isinstance(plate_objects, dict) or not plate_objects:
                    continue
                tallies = tally_objects(plate_objects)
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

    # AFTER the archive backfill, never before: rule D reads exactly the rows it
    # has just written. And by now the library backfill at the TOP of this
    # function has given every product whose FILE was the empty one its real
    # printed parts, so rule D correctly leaves those alone — a product we can
    # measure from its own file is not a history-only product. Reordering any of
    # the three leaves a product part-less, silently.
    await seed_history_only_products(session_factory)

    # The hand-over is spent. Dropped LAST, so a ``seed()`` that raises anywhere
    # above leaves it in place for the re-entry the runner will make. It is also
    # the WORKLIST that re-entry runs on: the backfill commits per chunk and so
    # has nothing left to report the second time round, and these rows are then
    # the only surviving record of which files still owe their products parts.
    # Applying the factor twice is not possible (the parts step only adds a key
    # nothing covers yet); losing it before it has been applied once would be.
    #
    # ⚠️ Known residual, accepted: a file the backfill above could NOT reach (its
    # share was offline during the upgrade) gets its objects from the boot sweep
    # in ``main.py`` at some later start, and by then this table is gone — so the
    # parts derived there carry the plate's instance count WITHOUT the legacy
    # plan's ``copies`` factor. It is the narrow case (an unreachable mount at
    # upgrade time, on an install converting a legacy plan) and the alternative —
    # keeping a scratch table alive across restarts for ever — is worse.
    async with session_factory() as session:
        await session.execute(text(f"DROP TABLE IF EXISTS {_PENDING_COPIES}"))
        await session.commit()


async def _rescale_purchased_parts(session, product_id: int, units: int) -> None:
    """Purchased totals ÷ the units the line now orders (rule D's half of it).

    ``project_procurement.quantity_acquired`` is left alone on purpose: it is an
    absolute total on both sides of this change, and the need it is compared
    against is ``qty_per_unit × quantity``.
    """
    rows = (
        await session.execute(
            text("SELECT id, name, qty_per_unit FROM product_parts WHERE product_id = :p AND kind = 'purchased'"),
            {"p": product_id},
        )
    ).all()
    for part_id, part_name, qty_per_unit in rows:
        total = int(qty_per_unit or 0)
        if total <= 0:
            # "Don't measure this one" — ``order_metrics`` skips a purchased
            # part at 0, so scaling it up to 1 would start counting a need the
            # operator switched off.
            continue
        per_unit = _per_unit_from_total(total, units, f"purchased part {part_name!r} (rule D)")
        if per_unit != total:
            await session.execute(
                text("UPDATE product_parts SET qty_per_unit = :q WHERE id = :id"),
                {"q": per_unit, "id": part_id},
            )


async def seed_history_only_products(session_factory) -> None:
    """Rule D (spec §Migration 6b): a part-less product takes its parts from history.

    An order whose library files were deleted years ago converts to a product
    with no plates and therefore no printed parts — nothing to measure, no
    progress, a page that shows only a name. But its archives are still there,
    and after the backfill above each of them knows what it printed. So a
    product with **no printed parts** whose line's completed archives do carry
    ``print_archive_parts`` rows gets one part per distinct ``name_key``,
    ``auto=False`` (there is no file left for a sync to re-derive them from, so
    they must never be rewritten) and ``aliases=[name_key]``.

    The share is the gcd-normalised ratio of the usable totals (quantity minus
    defective), and the line quantity is Σ usable ÷ Σ share — which is the old
    ``target_parts_count`` whenever the prints were actually run to the target.
    ⚠️ The gcd is why one defective print skews a whole composition: 780 and
    780 usable give ``gcd = 780`` and a clean ``1 + 1 per unit × 780``, but a
    single scrapped part turning them into 780 and 779 gives ``gcd = 1``, and
    the product is then recorded as needing 780 + 779 of the two parts for ONE
    unit, ordered × 1. The totals are still faithful — it is the split between
    "per unit" and "how many units" that collapses — and the composition table
    is where an operator fixes it. That is the only number available here:
    ``upgrade`` drops the legacy
    columns in the same transaction that creates the products, so by the time
    ``seed`` runs there is no target left to read. Each line is measured
    against its OWN history, so its quantity stays self-consistent even when
    the first line was the one that named the parts.

    Raising a line from × 1 to × Q also divides that product's PURCHASED parts
    by Q (``_rescale_purchased_parts``), for the same reason ``upgrade`` does:
    their totals were the whole project's and the order multiplies them now.
    Once per product, and only when the quantity actually moved.

    Idempotent and self-contained — it depends on no legacy table or column, so
    it also repairs a database that a previous run of this migration already
    converted (m158 is not the head migration, so ``DEBUG=true`` will not
    re-enter it; this function is what gets called by hand instead). Two
    guards keep the repair from overwriting decisions: a product that already
    has any printed part is skipped whole, and a line whose quantity is no
    longer 1 has been edited by an operator and keeps its number.
    """
    async with session_factory() as session:
        product_ids = [
            pid
            for (pid,) in (
                await session.execute(
                    text(
                        "SELECT p.id FROM products p WHERE NOT EXISTS "
                        "(SELECT 1 FROM product_parts pp WHERE pp.product_id = p.id AND pp.kind = 'printed') "
                        "ORDER BY p.id"
                    )
                )
            ).all()
        ]
        for product_id in product_ids:
            line_ids = [
                lid
                for (lid,) in (
                    await session.execute(
                        text("SELECT id FROM project_lines WHERE product_id = :p ORDER BY sort_order, id"),
                        {"p": product_id},
                    )
                ).all()
            ]
            written = False
            scaled = False
            for line_id in line_ids:
                totals = (
                    await session.execute(
                        text(
                            "SELECT pap.name_key, MIN(pap.name) AS name, "
                            "SUM(pap.quantity - COALESCE(pap.defective, 0)) AS usable "
                            "FROM print_archives pa JOIN print_archive_parts pap ON pap.archive_id = pa.id "
                            "WHERE pa.project_line_id = :l AND pa.status = 'completed' AND pa.deleted_at IS NULL "
                            "GROUP BY pap.name_key "
                            # The expression, not the alias: PostgreSQL does not
                            # accept a select alias in HAVING.
                            "HAVING SUM(pap.quantity - COALESCE(pap.defective, 0)) > 0 "
                            "ORDER BY pap.name_key"
                        ),
                        {"l": line_id},
                    )
                ).all()
                if not totals:
                    continue
                usable = {key: int(total) for key, _name, total in totals}
                share = gcd(*usable.values())
                per_unit = {key: value // share for key, value in usable.items()}
                if not written:
                    for sort_order, (key, part_name, _total) in enumerate(totals):
                        await session.execute(
                            text(
                                "INSERT INTO product_parts (product_id, kind, name, name_key, qty_per_unit, "
                                "aliases, auto, sort_order) "
                                "VALUES (:pid, 'printed', :n, :k, :q, :al, :auto, :so)"
                            ),
                            {
                                "pid": product_id,
                                "n": part_name or key,
                                "k": key,
                                "q": per_unit[key],
                                "al": json.dumps([key]),
                                "auto": 0 if is_sqlite() else False,
                                "so": sort_order,
                            },
                        )
                    written = True
                quantity = max(1, sum(usable.values()) // sum(per_unit.values()))
                raised = await session.execute(
                    text("UPDATE project_lines SET quantity = :q WHERE id = :l AND quantity = 1"),
                    {"q": quantity, "l": line_id},
                )
                # Raising the quantity multiplies every purchased requirement by
                # it, so the totals the BOM left behind have to come down by the
                # same factor — exactly as ``upgrade`` does it. Once per product:
                # the parts are the product's, not the line's, and a second line
                # must not divide them again. A quantity that did NOT move (an
                # operator's edit) scales nothing.
                if not scaled and quantity > 1 and (raised.rowcount or 0) > 0:
                    await _rescale_purchased_parts(session, product_id, quantity)
                    scaled = True
                logger.info(
                    "m158 seed: product %d gets %d part(s) from history (line %d x %d)",
                    product_id,
                    len(usable),
                    line_id,
                    quantity,
                )
        await session.commit()
