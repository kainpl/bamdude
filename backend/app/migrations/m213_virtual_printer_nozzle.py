"""Persist the unbound VP's advertised main-nozzle diameter and flow (GH106).

Bound VPs continue using real hardware reports. The defaults preserve the
old unbound stub; portable backups carry these ordinary model columns too.
"""

from backend.app.migrations.helpers import add_column

version = 213
name = "virtual_printer_nozzle"


async def upgrade(conn):
    await add_column(conn, "virtual_printers", "nozzle_diameter VARCHAR(3) NOT NULL DEFAULT '0.4'")
    await add_column(conn, "virtual_printers", "nozzle_flow VARCHAR(20) NOT NULL DEFAULT 'standard'")
