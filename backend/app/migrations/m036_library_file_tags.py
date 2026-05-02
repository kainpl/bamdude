"""Add ``library_files.file_tags`` JSON array + backfill from existing fields.

The frontend file-manager has been rendering three independent badges
per file (primary type, multi-plate, swap-compatible) plus a separate
``SourceBadge`` component reading ``source_type``. Each badge had its
own inline condition in two places (FileCard grid + FileListActions
list) and the conditions drifted — the grid view, until very recently,
didn't render the Slice action at all.

m036 unifies the badge surface into a single ``file_tags: list[str]``
column on ``library_files``. Tags emitted today (stable left-to-right
order in the UI):

- Format: ``3mf`` / ``gcode`` / ``stl`` / ``step``. Sliced 3MFs carry
  both ``gcode`` and ``3mf`` (composite badge restoring the visual
  distinction m035 collapsed in the primary ``file_type``).
- Structure: ``multiplate`` (set when ``file_metadata.is_multi_plate``
  or ``len(file_metadata.plates) > 1``) and ``swap`` (mirrors the
  ``swap_compatible`` boolean).
- Provenance: ``sliced`` / ``makerworld`` / ``project`` (mirrors
  ``source_type``).

The four denormalised inputs (``file_type``, ``file_metadata``,
``source_type``, ``swap_compatible``) stay as separate columns — kept
deliberately for fast filter / FTS / Telegram-bot listings. ``file_tags``
is the secondary, computed projection consumed by the UI badge layer.

Backfill: we re-run :func:`compute_file_tags` on every row, which means
this migration will repopulate any future drift if it's re-applied
manually (the column is NOT NULL with default ``[]`` so a re-run is
always safe).
"""

from backend.app.migrations.helpers import add_column
from backend.app.services.library_helpers import compute_file_tags

version = 36
name = "library_file_tags"


async def upgrade(conn):
    # SQLite stores JSON columns as TEXT; PostgreSQL treats them as
    # ``json`` / ``jsonb`` automatically. The literal ``[]`` default
    # round-trips through both backends. NOT NULL keeps the runtime
    # write-side simple (no ``or []`` defensive copies needed).
    added = await add_column(conn, "library_files", "file_tags TEXT NOT NULL DEFAULT '[]'")
    if not added:
        # Column already there — bail out before running the (potentially
        # expensive) backfill. ``add_column`` is idempotent so a re-run
        # of m036 against an upgraded DB no-ops cleanly.
        return


async def seed(session_factory):
    """Backfill ``file_tags`` for every existing row using the same
    helper the runtime write paths use. Runs in chunks of 500 to keep
    SQLite's WAL from ballooning on large libraries.
    """
    import json

    from sqlalchemy import select, update

    from backend.app.models.library import LibraryFile

    async with session_factory() as db:
        # Stream rows in batches; an install with 50k library files is
        # plausible on a long-running farm and a single in-memory load
        # would needlessly spike resident memory during upgrade.
        offset = 0
        batch_size = 500
        while True:
            rows = (
                await db.execute(
                    select(
                        LibraryFile.id,
                        LibraryFile.filename,
                        LibraryFile.file_type,
                        LibraryFile.file_metadata,
                        LibraryFile.source_type,
                        LibraryFile.swap_compatible,
                        LibraryFile.file_tags,
                    )
                    .order_by(LibraryFile.id)
                    .offset(offset)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                # Skip rows that already carry a non-empty tag list so a
                # re-run of m036 (e.g. after a code rollback that wiped
                # the column) doesn't clobber tags freshly written by
                # the runtime path between m036 application and seed run.
                existing = row.file_tags
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except (ValueError, TypeError):
                        existing = []
                if existing:
                    continue
                tags = compute_file_tags(
                    filename=row.filename,
                    file_type=row.file_type,
                    file_metadata=row.file_metadata,
                    source_type=row.source_type,
                    swap_compatible=bool(row.swap_compatible),
                )
                await db.execute(update(LibraryFile).where(LibraryFile.id == row.id).values(file_tags=tags))
            await db.commit()
            offset += batch_size
            if len(rows) < batch_size:
                break
