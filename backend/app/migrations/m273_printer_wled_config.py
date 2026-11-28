"""A printer's optional WLED preset mapping (N02, upstream #3124).

``printers.wled_config`` holds one printer's WLED address, the preset to show for each printer
state and the Finished timeout; ``services/wled.py`` switches those presets as the printer's status
changes. Nullable with no default and no seed: no printer drives a WLED until the operator sets
one up. A database that ran Bambuddy 1.3.1 or later already has the column (upstream adds it the
same way), so ``add_column`` leaves it — and its configurations — as they are.
"""

from backend.app.migrations.helpers import add_column

version = 273
name = "printer_wled_config"


async def upgrade(conn):
    await add_column(conn, "printers", "wled_config JSON")
