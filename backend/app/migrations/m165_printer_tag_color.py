"""m165: a colour on a printer tag.

Design: docs/superpowers/specs/2026-09-06-tags-and-stagger-second-pass-design.md (Decision 2).

One nullable ``VARCHAR(7)`` — ``#rrggbb`` chosen from a fixed palette in the UI,
NULL = the neutral chip. No seed: a tag has no colour until the operator picks one.
``create_all`` gives a fresh install the column through the model; this is the
path an existing database walks, and ``add_column`` is idempotent.
"""

from backend.app.migrations.helpers import add_column

version = 165
name = "printer_tag_color"


async def upgrade(conn):
    await add_column(conn, "printer_tags", "color VARCHAR(7)")
