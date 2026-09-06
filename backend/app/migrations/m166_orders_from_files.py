"""m166: orders from files — product origin and the plate-product identity.

Design: docs/superpowers/specs/2026-09-06-orders-from-files-design.md (Slice A).

``origin`` defaults to ``catalog``, which is what every existing product is.
The two identity columns are NULL for everything that exists today; only an
``adhoc_plate`` product fills them, and the PARTIAL unique index is what keeps
one plate of one file from ever having two products. Both SQLite and
PostgreSQL accept ``CREATE UNIQUE INDEX IF NOT EXISTS … WHERE …``; the model
declares the same index, so a fresh install gets it from ``create_all``.

No FK on ``origin_file_id`` here: SQLite's ``ALTER TABLE ADD COLUMN`` with
``REFERENCES`` is legal but the codebase never enforces FKs, and the model
carries the constraint for fresh installs. No seed.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 166
name = "orders_from_files"


async def upgrade(conn):
    await add_column(conn, "products", "origin VARCHAR(16) NOT NULL DEFAULT 'catalog'")
    await add_column(conn, "products", "origin_file_id INTEGER")
    await add_column(conn, "products", "origin_plate_index INTEGER")
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_products_origin_plate "
            "ON products (origin_file_id, origin_plate_index) WHERE origin = 'adhoc_plate'"
        )
    )
