"""A wear cost per printing hour: the printer's rate and the figure a print was charged.

``printers.wear_cost_per_hour`` is the operator's rate (money per printing hour); NULL
or 0 means the feature is off for that printer, which is every printer after this
migration. ``print_archives.wear_cost`` is the figure stamped when a print ends with a
measured duration, and ``print_archives.wear_rate`` the rate that was applied, so the
figure can still be explained after the rate changes.

All three are nullable with no default and there is no seed: existing prints keep NULL
(no figure, never a zero) and nothing is backfilled -- a rate applies to prints that
end after it was set. ``services/wear_cost.py`` is the only writer of the two archive
columns.
"""

from backend.app.migrations.helpers import add_column

version = 246
name = "wear_cost"


async def upgrade(conn):
    await add_column(conn, "printers", "wear_cost_per_hour FLOAT")
    await add_column(conn, "print_archives", "wear_cost FLOAT")
    await add_column(conn, "print_archives", "wear_rate FLOAT")
