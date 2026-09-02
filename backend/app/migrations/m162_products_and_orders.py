"""m162: projects become orders; products, customers, order lines appear.

Design: docs/superpowers/specs/2026-09-02-projects-redesign-design.md.

Everything happens in ``upgrade(conn)`` — one transaction (FK off on SQLite),
the m044 precedent — so a conversion that fails half-way rolls back whole and
the legacy tables are still there for the next attempt. There is no ``seed``.

Order of work:

1. create the new tables and columns (guarded; fresh installs already have
   them from ``create_all()``);
2. IF the legacy pivot ``library_file_projects`` still exists, convert every
   project: one product, its files/folders/plates/parts, BOM → purchased
   parts + procurement, one order line ``× 1``, archives + queue rows get the
   line, ``budget → price``, ``archived → completed``; templates become
   products only and their attachments are COPIED to the product directory
   (never moved — see ``_copy_template_attachments``);
3. drop the five legacy tables and the six legacy ``projects`` columns.

Because step 3 removes the marker step 2 keys on, a re-run (``DEBUG=true``)
finds nothing to convert and touches no data. Named columns everywhere.
"""

import json
import logging
import shutil
from collections import Counter
from pathlib import Path

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, column_exists, get_table_columns, recreate_table, table_exists
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_files import product_attachments_dir

logger = logging.getLogger(__name__)

version = 162
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


def _load_meta(raw) -> dict:
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


async def _insert_returning_id(conn, sql: str, params: dict) -> int:
    """INSERT … RETURNING id — SQLite ≥ 3.35 (Python 3.12 bundles 3.4x) and PostgreSQL."""
    return (await conn.execute(text(sql + " RETURNING id"), params)).scalar_one()


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

    plan_rows = (
        await conn.execute(
            text(
                "SELECT p.library_file_id, p.plate_index, p.copies, f.file_metadata "
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
    for file_id, plate_index, copies, raw_meta in plan_rows:
        if (file_id, plate_index) not in seen_plates:
            seen_plates.add((file_id, plate_index))
            await conn.execute(
                text("INSERT INTO product_plates (product_id, library_file_id, plate_index) VALUES (:pid, :f, :pl)"),
                {"pid": product_id, "f": file_id, "pl": plate_index},
            )
        try:
            counts, names = _plate_key_counts(_load_meta(raw_meta), plate_index)
        except Exception:  # noqa: BLE001 — one corrupt file_metadata must not abort the upgrade
            logger.warning(
                "m162: skipped metadata of file %s while converting project %s", file_id, project_id, exc_info=True
            )
            continue
        for key, n in counts.items():
            yield_by_key[key] += (copies or 1) * n
            first_count.setdefault(key, n)
            display.setdefault(key, names[key])

    targets = {
        r[1]: (r[0], r[2])
        for r in (
            await conn.execute(
                text("SELECT name, name_key, target_qty FROM project_parts WHERE project_id = :p"), {"p": project_id}
            )
        ).all()
    }
    sort_order = 0
    for key in sorted(set(targets) | set(yield_by_key)):
        if key in targets:
            part_name, target = targets[key]
            if target and target > 0:
                qty, auto = target, False
            elif target == 0:
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
            "INSERT INTO project_lines (project_id, product_id, quantity, sort_order) VALUES (:p, :pid, 1, 0)",
            {"p": project_id, "pid": product_id},
        )
    used_keys: set[str] = set()
    for bom_name, needed, acquired, price, url, remarks in bom_rows:
        key = PURCHASED_KEY_PREFIX + " ".join((bom_name or "").split()).lower()
        if key in used_keys:  # two BOM rows with the same name — keep the first
            continue
        used_keys.add(key)
        part_id = await _insert_returning_id(
            conn,
            "INSERT INTO product_parts "
            "(product_id, kind, name, name_key, qty_per_unit, unit_price, sourcing_url, remarks, auto, sort_order) "
            "VALUES (:pid, 'purchased', :n, :k, :q, :price, :url, :rem, :auto, :so)",
            {
                "pid": product_id,
                "n": bom_name,
                "k": key,
                "q": needed or 1,
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
    rows = (await conn.execute(text(f"SELECT {cols} FROM projects ORDER BY id"))).mappings().all()

    converted = 0
    for row in rows:
        await _convert_one_project(conn, dict(row), is_template=bool(row["is_template"]))
        converted += 1
    if has_budget:
        await conn.execute(text("UPDATE projects SET price = budget WHERE budget IS NOT NULL AND price IS NULL"))
    await conn.execute(text("UPDATE projects SET status = 'completed' WHERE status = 'archived'"))
    logger.info("m162: converted %d project(s) into products + order lines", converted)


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


async def upgrade(conn):
    await _create_tables(conn)
    await _convert_legacy(conn)
    await _drop_legacy(conn)
