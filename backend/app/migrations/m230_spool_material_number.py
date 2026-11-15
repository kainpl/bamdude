"""A material number on a spool (upstream 71d4b2f70, #2994).

``spool.material_number VARCHAR(64)`` — the operator's own article code for a
filament ("15" = Acme PLA Basic Black, 1 kg): free text, nullable, NOT unique,
shared by the spools of one filament. It sits beside ``lot`` (a spool's place in
one purchase bundle), which it does not replace.

No backfill: no existing spool has a number. A new spool of an already-numbered
filament inherits it at creation (``inventory_service.inherit_material_number``).
"""

from backend.app.migrations.helpers import add_column

version = 230
name = "spool_material_number"


async def upgrade(conn):
    await add_column(conn, "spool", "material_number VARCHAR(64)")
