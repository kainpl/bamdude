"""Decide "is this 3MF sliced" by looking inside the existing files.

``compute_file_tags`` derived the ``gcode`` tag from ``detect_file_type``, which
reads the **filename**. So the tag answered "is it NAMED like a sliced file",
and that tag is what gates every "can this be printed" affordance — the web
file manager, and from this release the Telegram bot.

New uploads now record the answer at parse time. This backfills the rows that
predate that, by opening each 3MF and asking whether it contains
``Metadata/*.gcode``.

⚠️ **Reads only the ZIP central directory** (``namelist``) and extracts
nothing — the same read ``parse_plates_from_3mf`` already performs to discover
plates. That is what makes it affordable across a library of thousands rather
than a migration nobody dares run.

⚠️ **A file we cannot open is left alone**, not marked unsliced. Missing file,
external mount not attached, corrupt archive — none of those are evidence about
the contents, and writing ``false`` there would be inventing an answer. Such a
row keeps the filename rule it has always had.

No schema change: the flag lives in the existing ``file_metadata`` JSON.
"""

import logging

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

version = 137
name = "sliced_by_content_backfill"


async def upgrade(conn):
    """No DDL — this migration only rewrites data."""
    return


async def seed(session_factory):
    from backend.app.core.config import settings
    from backend.app.models.library import LibraryFile
    from backend.app.services.library_helpers import (
        SLICED_GCODE_META_KEY,
        compute_file_tags,
        sliced_gcode_in_3mf,
    )

    checked = flagged = retagged = skipped = 0

    async with session_factory() as session:
        # ⚠️ Columns by name, never ``select(LibraryFile)``: a later migration
        # that adds a column breaks a whole-entity select in this one, mid-chain
        # on somebody's upgrade.
        rows = (
            await session.execute(
                select(
                    LibraryFile.id,
                    LibraryFile.filename,
                    LibraryFile.file_path,
                    LibraryFile.file_type,
                    LibraryFile.file_metadata,
                    LibraryFile.source_type,
                    LibraryFile.swap_compatible,
                    LibraryFile.file_tags,
                    LibraryFile.is_external,
                ).where(
                    LibraryFile.deleted_at.is_(None),
                    LibraryFile.filename.ilike("%.3mf"),
                )
            )
        ).all()

        for row in rows:
            (fid, filename, file_path, file_type, meta, source_type, swap_compatible, before, is_external) = row
            checked += 1

            if not file_path:
                skipped += 1
                continue
            # Managed rows store a base_dir-relative path; external ones store
            # the absolute mount path, same shape the folder scan produces.
            full_path = file_path if is_external else str(settings.base_dir / file_path)

            sliced = sliced_gcode_in_3mf(full_path)
            if sliced is None:
                skipped += 1
                continue

            metadata = dict(meta) if isinstance(meta, dict) else {}
            metadata[SLICED_GCODE_META_KEY] = sliced
            flagged += 1

            tags = compute_file_tags(
                filename=filename,
                file_type=file_type,
                file_metadata=metadata,
                source_type=source_type,
                swap_compatible=bool(swap_compatible),
            )
            if sorted(before or []) != sorted(tags):
                retagged += 1
                if sliced is False and "gcode" in (before or []):
                    # Worth naming individually: this row offered a Print
                    # button for a file the printer cannot parse.
                    logger.info("m137: %r was tagged printable but holds no sliced G-code", filename)

            # ⚠️ ORM update, not raw SQL with ``json.dumps``. These are JSON
            # columns, and SQLAlchemy is what knows how each backend wants them
            # written — hand PostgreSQL a pre-serialised string and it stores a
            # JSON *string*, so every later reader gets a str where it expects a
            # dict. SQLite would never have shown it. Same shape m036 uses.
            await session.execute(
                update(LibraryFile).where(LibraryFile.id == fid).values(file_metadata=metadata, file_tags=tags)
            )

        await session.commit()

    logger.info(
        "m137: inspected %d 3MF file(s) — %d flagged, %d re-tagged, %d left alone (unreadable or pathless)",
        checked,
        flagged,
        retagged,
        skipped,
    )
