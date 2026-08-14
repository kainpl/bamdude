"""Which medium records the timelapse, chosen per print.

``timelapse_storage TEXT NULL`` — ``'internal'`` / ``'external'``, mirroring
BambuStudio's own per-job picker. Studio keeps this in the Send dialog and
sends it with the job as ``cfg`` bit 2; there is **no setting on the printer**
to configure instead, which is why this is a property of the job and not of the
machine.

Added to both queue tables: ``auto_queue_items`` is what the distributor copies
the per-printer row from, so a column on only one of them would drop the choice
on the way through — the same reasoning as ``selected_macro_ids`` (m135).

NULL on every existing row, and NULL keeps today's behaviour exactly: BamDude
sent ``cfg: "0"`` unconditionally, which leaves the medium to the printer. No
seed, because no operator has chosen anything yet and inventing "internal" for
old rows would silently move where recordings land on machines that have been
writing to a card for months.
"""

from backend.app.migrations.helpers import add_column

version = 136
name = "timelapse_storage_choice"


async def upgrade(conn):
    await add_column(conn, "print_queue", "timelapse_storage TEXT")
    await add_column(conn, "auto_queue_items", "timelapse_storage TEXT")
