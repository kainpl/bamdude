"""Build-plate type on print archives.

3MF metadata carries ``curr_bed_type`` (Cool Plate / Cool Plate SuperTack /
Engineering Plate / High Temp Plate / Textured PEI Plate / Smooth PEI Plate)
under ``Metadata/slice_info.config`` per-plate. The archive card surfaces
this so operators don't have to remember which plate matched a re-print —
the bed icon is rendered next to the printer / model name with the full
plate name in the hover tooltip.

Fresh installs get the column from ``backend/app/models/archive.py`` via
``Base.metadata.create_all``. Existing rows backfill to NULL — the card
omits the icon when ``bed_type`` is unset (no broken-image placeholder
on pre-feature archives). Operators can re-populate any archive via the
existing per-row **Rescan** button, which now also reads ``curr_bed_type``
from the on-disk 3MF.

Adapted from upstream Bambuddy ``79d54a8d`` (#1253).
"""

from backend.app.migrations.helpers import add_column

version = 57
name = "archive_bed_type"


async def upgrade(conn):
    await add_column(conn, "print_archives", "bed_type VARCHAR(64)")
