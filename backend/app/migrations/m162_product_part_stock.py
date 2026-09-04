"""m162: free stock of product parts — the movements ledger.

Design: docs/superpowers/specs/2026-09-04-projects-redesign-pass8-part-stock-design.md
(Decision 1 for the shape, Decision 7 for this migration).

One table, one index, **no backfill**. The absence of a backfill is the
decision, not an omission: an order-less archive from last year may have been
shipped, scrapped or given away, and nothing in the database says which — so
inventing a starting balance out of the archive history would hand the
operator a number that looks measured and is not. Stock starts empty and fills
from the prints and the buttons that come after this migration; the archive
editor's one-off "count this print into stock" covers the individual old
prints somebody can actually vouch for.

There is no ``seed()`` for the same reason.

DDL is written dialect-neutral (m158's product tables are the pattern), even
though the table is in ``Base.metadata`` and ``create_all`` therefore builds it
on every fresh install before this migration is reached. The guard below then
finds it and does nothing; the CREATE below is the path an EXISTING database
walks, and on PostgreSQL that path must parse — see
``tests/unit/test_migration_ddl_dialects.py`` for the install this rule was
written after.
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 162
name = "product_part_stock"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME DEFAULT CURRENT_TIMESTAMP" if sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

    if not await table_exists(conn, "product_part_stock_movements"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE product_part_stock_movements (
                id {pk},
                product_part_id INTEGER NOT NULL REFERENCES product_parts(id) ON DELETE CASCADE,
                delta INTEGER NOT NULL,
                reason VARCHAR(32) NOT NULL,
                project_line_id INTEGER REFERENCES project_lines(id) ON DELETE SET NULL,
                archive_id INTEGER REFERENCES print_archives(id) ON DELETE SET NULL,
                note TEXT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at {ts} NOT NULL
            )
            """
        )
    # Outside the table guard and IF NOT EXISTS (both dialects): a fresh
    # install already has this index from ``create_all``, and a database that
    # somehow has the table without it still ends up with it. Every read of the
    # ledger is "this part, newest first", so this one index serves them all.
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_product_part_stock_movements_part_created "
        "ON product_part_stock_movements (product_part_id, created_at)"
    )
