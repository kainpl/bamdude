"""Add ``weight_used_baseline`` anchor to the ``spool`` table.

Backs the new "Reset usage to 0" eraser action (#1390 + #1390-followup).

The previous "Reset usage" cut zeroed ``weight_used`` directly, which
correctly cleared the displayed "Total Consumed" stat (`weight_used`) —
but ``weight_used`` is also the basis for the displayed *remaining*
(`label_weight - weight_used`), so the reset silently inflated a
544 g spool's remaining all the way back to ``label_weight`` (1000 g).

Split the two concerns: ``weight_used`` stays the running consumption
counter; ``weight_used_baseline`` is the anchor stamped by the reset
action. The Inventory page now shows::

    consumed  = max(0, weight_used - weight_used_baseline)
    remaining = label_weight - weight_used

so resetting stamps ``baseline = weight_used`` (consumed reads 0) and
remaining is preserved. Mirrors Spoolman's existing split between
``used_weight`` and ``remaining_weight``.

Default 0 — existing spools render unchanged (consumed = weight_used).

Upstream Bambuddy #1390 / commit ``e61a454a``.
"""

from backend.app.migrations.helpers import add_column

version = 75
name = "spool_weight_used_baseline"


async def upgrade(conn):
    # ``REAL`` is the SQLite spelling; PostgreSQL's ``add_column`` helper
    # leaves the type token alone and ``REAL`` is a valid PG type alias for
    # ``float4`` (good enough for grams to a tenth-of-a-gram resolution).
    await add_column(conn, "spool", "weight_used_baseline REAL DEFAULT 0 NOT NULL")
