"""Computed file badges become catalog rows marked ``is_system`` (tags cycle).

``library_files.file_tags`` (m036) and ``library_tags`` (m095) answered the same
question through two storage layers and two filters: "show me files marked X".
The first filtered client-side with state in localStorage, the second
server-side through ``tag_ids``. System tags are now rows in the same catalog,
so one filter serves both and the management dialog can show one list.

``file_tags`` is deliberately NOT dropped. It stays as an explicitly derived
cache written by the same function that writes the associations
(``library_helpers.sync_system_tags``), because the hot-path predicates —
``isSliced`` on the frontend, preview-tab visibility in ``routes/library.py`` —
read a column on a row they already have and should not start depending on the
state of a join.

The single-column unique index on ``name_key`` is replaced by a composite over
``(name_key, is_system)``: an install where somebody already created a tag named
"sliced" has to migrate without either renaming their data or prefixing every
system row forever.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 128
name = "library_system_tags"

# The stable identifiers ``compute_file_tags`` emits, with the English fallback
# name. Spelled out here rather than imported from the service: a migration must
# describe the world as it was when it was written, and the vocabulary may
# change later. The label a user sees comes from i18n, not from this table.
SYSTEM_TAGS = [
    ("3mf", "3MF"),
    ("gcode", "GCODE"),
    ("stl", "STL"),
    ("obj", "OBJ"),
    ("step", "STEP"),
    ("project", "Project"),
    ("geometry", "Geometry"),
    ("multiplate", "Multi-plate"),
    ("swap", "Swap"),
    ("sliced", "Sliced"),
    ("makerworld", "MakerWorld"),
]


async def upgrade(conn):
    await add_column(conn, "library_tags", "is_system BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "library_tags", "code VARCHAR(32)")
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_library_tags_name_key_is_system ON library_tags (name_key, is_system)"
        )
    )
    await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_library_tags_code ON library_tags (code)"))
    # Dropped LAST: until the composite index exists, this is the only thing
    # keeping the catalog from growing duplicate names.
    await conn.execute(text("DROP INDEX IF EXISTS ix_library_tags_name_key"))


async def seed(session_factory):
    """Insert the system rows, then backfill one association per (file, code).

    Both halves are idempotent — ``DEBUG=true`` re-runs the latest migration on
    every startup, and this one walks the whole library.
    """
    import json

    from sqlalchemy import select

    from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryTag

    # Tables, not mapped attributes. ``select(LibraryTag.code)`` would trigger
    # configure_mappers(), and a migration must not depend on the WHOLE model
    # graph having been imported — the registry is assembled inside init_db(),
    # not by importing the models package, so anything running earlier or in
    # isolation would fail on an unrelated model it never asked about.
    tags = LibraryTag.__table__
    file_tags_assoc = LibraryFileTag.__table__
    files = LibraryFile.__table__

    async with session_factory() as db:
        # --- the eleven catalog rows -------------------------------------
        existing_codes = set((await db.execute(select(tags.c.code).where(tags.c.code.is_not(None)))).scalars())
        new_rows = [
            {"name": label, "name_key": code, "is_system": True, "code": code}
            for code, label in SYSTEM_TAGS
            if code not in existing_codes
        ]
        if new_rows:
            await db.execute(tags.insert(), new_rows)
            await db.commit()

        tag_id_by_code = dict(
            (await db.execute(select(tags.c.code, tags.c.id).where(tags.c.is_system.is_(True)))).all()
        )

        # --- one association per (file, code) ----------------------------
        # Batched for the same reason m036 batches: a 50k-file library would
        # otherwise balloon SQLite's WAL during upgrade. Trashed files are
        # included — they are still in the library and can be restored, and
        # skipping them would leave a restored file invisible to every filter.
        offset = 0
        batch_size = 500
        while True:
            rows = (
                await db.execute(
                    select(files.c.id, files.c.file_tags).order_by(files.c.id).offset(offset).limit(batch_size)
                )
            ).all()
            if not rows:
                break

            file_ids = [row.id for row in rows]
            already = set(
                (
                    await db.execute(
                        select(file_tags_assoc.c.file_id, file_tags_assoc.c.tag_id).where(
                            file_tags_assoc.c.file_id.in_(file_ids)
                        )
                    )
                ).all()
            )

            to_insert = []
            for row in rows:
                codes = row.file_tags
                if isinstance(codes, str):
                    try:
                        codes = json.loads(codes)
                    except (ValueError, TypeError):
                        codes = []
                for code in codes or []:
                    tag_id = tag_id_by_code.get(code)
                    # An unknown code means the vocabulary moved on since this
                    # row was written. Skipping is right: inventing a catalog
                    # row for it would resurrect a retired tag.
                    if tag_id is not None and (row.id, tag_id) not in already:
                        to_insert.append({"file_id": row.id, "tag_id": tag_id})

            if to_insert:
                await db.execute(file_tags_assoc.insert(), to_insert)
            await db.commit()

            offset += batch_size
            if len(rows) < batch_size:
                break
