"""Per-VP opt-in: reuse the slicer's own AMS pick on a reprint (#2700).

Two spools of the same red PLA sit in different slots. The user picks one in
Bambu Studio; the slicer resolves that to a concrete tray and sends it through
the Virtual Printer. Today we throw the resolved pick away and re-derive a slot
from the file's static type/colour — which cannot tell the two spools apart.

Storing the resolved mapping fixes that, and costs something real, which is why
this is a per-VP toggle rather than a behaviour change. A queue item that already
carries a mapping makes ``_ensure_ams_mapping`` return early, so
``_compute_ams_mapping_for_printer`` never runs — and that function is where
``prefer_lowest_filament``, the AMS-Filament-Backup gate (#1766), the
inventory-remain overrides (#1508) and the FTS routing rule (#2186) live. With
the toggle **off**, which is the default, every one of those still applies
exactly as it does today.

⚠️ FTS has no upstream counterpart — it is ours, and it is the one on this list
that can strand a print on a wrong-nozzle slot rather than merely pick a
different spool. Whatever the UI says this toggle switches off, it has to name.

Defaults to false, so nothing changes for an existing VP until somebody asks.
"""

from backend.app.migrations.helpers import add_column

version = 131
name = "vp_save_ams_mapping"


async def upgrade(conn):
    # BOOLEAN DEFAULT 0 — helpers translate the literal for PostgreSQL, which
    # rejects an integer default on a boolean column.
    await add_column(conn, "virtual_printers", "save_ams_mapping BOOLEAN DEFAULT 0")
