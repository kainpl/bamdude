"""Re-backfill ``library_files.file_tags`` after the m037 vocabulary change.

m036 emitted four format tags (``3mf`` / ``gcode`` / ``stl`` / ``step``) and
a provenance tag ``project`` for ``source_type`` starting with
``project_``. The chip-row filter that grew on top of those tags showed
that the practical question users ask isn't "what's the file extension"
but "is this thing ready to print, or do I still need to slice it?"

m037 collapses the format vocabulary into semantic groups:

- Sliced ``.gcode.3mf`` → ``gcode`` + ``3mf`` (composite, unchanged)
- Raw ``.gcode``        → ``gcode``
- Unsliced ``.3mf``     → ``project``         (was: ``3mf``)
- ``.stl`` / ``.obj`` / ``.step`` / ``.stp`` → ``geometry`` (was: ``stl``,
  ``step``; ``obj`` got no format tag at all)

The ``project`` tag is now reserved for "unsliced 3MF" (file-type
semantic). The old provenance interpretation (``source_type`` ⇒
``project_*``) is dropped — it had near-zero hit rate and conflated two
unrelated meanings under one chip.

Backfill: re-runs :func:`compute_file_tags` over every existing row and
**overwrites** the tag list (m036's seed skipped non-empty rows; m037
needs to replace them). The column itself was created in m036 so this
migration ships only a ``seed`` step.

Local upgrade procedure: just restart the backend. The migration runs on
startup like any other; in DEBUG mode it re-runs every restart so an
iteration loop sees fresh tags without manual intervention.
"""

import json

from sqlalchemy import select, update

from backend.app.models.library import LibraryFile
from backend.app.services.library_helpers import compute_file_tags

version = 37
name = "project_geometry_tags"


async def seed(session_factory):
    """Re-derive ``file_tags`` for every row using the m037 helper.

    Streams in batches of 500 to keep SQLite's WAL bounded on long
    histories (a busy farm can have tens of thousands of library rows).
    Unlike m036's seed this one always overwrites — the old format tags
    (``3mf`` / ``stl`` / ``step``) are gone from the helper's output, so
    skipping rows with non-empty ``file_tags`` would leave them stuck on
    obsolete vocabulary the frontend no longer styles.
    """
    async with session_factory() as db:
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
                new_tags = compute_file_tags(
                    filename=row.filename,
                    file_type=row.file_type,
                    file_metadata=row.file_metadata,
                    source_type=row.source_type,
                    swap_compatible=bool(row.swap_compatible),
                )
                # Cheap no-op guard: the column normalises to a JSON string
                # on SQLite, so compare against the parsed value to avoid
                # touching rows whose tags are already correct.
                existing = row.file_tags
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except (ValueError, TypeError):
                        existing = []
                if existing == new_tags:
                    continue
                await db.execute(update(LibraryFile).where(LibraryFile.id == row.id).values(file_tags=new_tags))
            await db.commit()
            offset += batch_size
            if len(rows) < batch_size:
                break
