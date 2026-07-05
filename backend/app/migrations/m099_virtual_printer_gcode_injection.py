"""Add ``gcode_injection`` to ``virtual_printers`` + ``auto_queue_items`` — per-VP
opt-in for auto-print G-code snippet injection (#1516).

Default off so existing ``gcode_snippets`` users don't silently start injecting
into VP / Studio-Send jobs after upgrading. Fresh installs get the columns from
the models' ``create_all``; this backfills existing DBs. The toggle applies to
BOTH VP dispatch modes (print_queue AND auto_queue), so both source tables carry
the flag: ``virtual_printers`` (the VP config) and ``auto_queue_items`` (the
pre-dispatch router row, whose value is copied onto the per-printer
``print_queue`` item at promotion time). ``print_queue.gcode_injection`` already
exists (m032) — not re-added here.

``add_column`` does not translate boolean defaults, so branch the literal
(SQLite ``0`` / PG ``FALSE``), mirroring m089 / m096.

Upstream Bambuddy b414af6b (inline ``run_migrations``; BamDude uses numbered
migrations, and extends the toggle to auto_queue for full VP-mode parity).
"""

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 99
name = "virtual_printer_gcode_injection"


async def upgrade(conn):
    false_literal = "FALSE" if is_postgres() else "0"
    await add_column(conn, "virtual_printers", f"gcode_injection BOOLEAN DEFAULT {false_literal}")
    await add_column(conn, "auto_queue_items", f"gcode_injection BOOLEAN DEFAULT {false_literal}")
