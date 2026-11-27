"""A Spoolman binding names its spool by number AND ``registered`` stamp.

``spoolman_slot_assignments`` (AMS slot → Spoolman spool) and
``spoolman_k_profile`` (Spoolman spool → printer K-profile) held the Spoolman
number alone. A number proves nothing: Spoolman hands the number of a deleted
spool to the next one (a plain ``INTEGER PRIMARY KEY`` on SQLite), and a
different database behind the address reuses the same numbers. A charge,
weight, drying stamp or K-profile then went to another spool.

``spool_registered`` is Spoolman's own ``registered`` stamp of the spool the
writer proved — set by the server at creation, never by a client
(``services/spoolman_spool_identity``). NULL is a row from before this
migration: the first confirming reader that holds the spool from Spoolman
writes its stamp (owner decision 2026-11-27, trust on first read). No data is
touched here.
"""

from backend.app.migrations.helpers import add_column

version = 266
name = "spoolman_binding_registered"


async def upgrade(conn):
    await add_column(conn, "spoolman_slot_assignments", "spool_registered VARCHAR(40)")
    await add_column(conn, "spoolman_k_profile", "spool_registered VARCHAR(40)")
