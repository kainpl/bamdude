"""Manyfold as a model source: its two permissions, and where an import's origin is recorded.

**Permissions.** ``manyfold:view`` / ``manyfold:import`` are granted once to every
group that holds the matching MakerWorld permission (a group that may browse or
import from one model source may from the other — upstream 3480d3943 does the
same), and both to the system Administrators group (O2: every permission is
seeded there in its migration; nothing re-syncs a group at start-up).

**``library_file_import_meta``.** One row per library file a model source
imported: which source, the model's and the file's id there (TEXT — a
self-hosted source's ids are not numbers), the model's title, its creator and
licence as they were at the import, and the model page's address. Provider
neutral on purpose; MakerWorld's own table (m056) is not touched and MakerWorld
writes nothing here.

``AUTOINCREMENT`` on SQLite keeps a row's id from passing to another import.
The foreign key is declared for PostgreSQL; SQLite runs no FK actions, so the
row is removed in code wherever a library file is purged
(``services/library_import_meta.forget_files``).
"""

from backend.app.migrations.helpers import table_exists

version = 235
name = "manyfold_permissions_import_meta"

_GRANTS = (("makerworld:view", "manyfold:view"), ("makerworld:import", "manyfold:import"))


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    if not await table_exists(conn, "library_file_import_meta"):
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
        ts = "DATETIME" if sqlite else "TIMESTAMP"
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE library_file_import_meta (
                id {pk},
                library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
                source_type VARCHAR(32) NOT NULL,
                source_model_id VARCHAR(128) NOT NULL,
                source_file_id VARCHAR(128),
                title VARCHAR(500),
                creator VARCHAR(255),
                license VARCHAR(128),
                canonical_url VARCHAR(1024),
                imported_at {ts} NOT NULL
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_library_file_import_meta_library_file_id "
        "ON library_file_import_meta (library_file_id)"
    )


async def seed(session_factory):
    from sqlalchemy import JSON, Boolean, Integer, String, column, select, table, update

    groups = table(
        "groups",
        column("id", Integer),
        column("name", String),
        column("is_system", Boolean),
        column("permissions", JSON),
    )
    async with session_factory() as db:
        rows = (await db.execute(select(groups.c.id, groups.c.name, groups.c.is_system, groups.c.permissions))).all()
        dirty = False
        for row in rows:
            held = list(row.permissions or [])
            wanted = [new for old, new in _GRANTS if old in held]
            if row.is_system and row.name == "Administrators":
                wanted = [new for _old, new in _GRANTS]
            missing = [permission for permission in wanted if permission not in held]
            if missing:
                await db.execute(update(groups).where(groups.c.id == row.id).values(permissions=held + missing))
                dirty = True
        if dirty:
            await db.commit()
