"""Add ``bamdude_forced_timelapse`` to ``print_archives``.

Tracks prints where BamDude forced the firmware to record a timelapse so the
finish-photo extractor (#1397) could pull the post-park-pre-drop frame. The
cleanup path uses this to delete the timelapse both locally and on the
printer's SD after extraction — the user didn't opt in to a timelapse
recording, only the framed finish photo.

Fresh installs get the column from the model's ``create_all``; this backfills
existing DBs. PostgreSQL rejects ``DEFAULT 0`` for BOOLEAN (default expression
of type integer), so branch the literal — SQLite accepts ``0``, PG needs
``FALSE``. (The ``add_column`` helper does not translate boolean defaults.)

Upstream Bambuddy #1397 / commit ``12d17bfb`` (which used an inline
``run_migrations`` step; BamDude uses numbered migrations).
"""

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 89
name = "archive_bamdude_forced_timelapse"


async def upgrade(conn):
    false_literal = "FALSE" if is_postgres() else "0"
    await add_column(conn, "print_archives", f"bamdude_forced_timelapse BOOLEAN DEFAULT {false_literal}")
