"""Give back the filament a zero AMS reading spent on paper.

For six days ``utils/filament_remaining`` answered ``0.0`` where BS answers
``nullopt``, and the AMS weight sync faithfully turned that into
``weight_used = label_weight``. One AMS push arriving two seconds after an MQTT
reconnect, reporting ``remain: 0`` for three slots at once, was enough to write
three 1 kg spools off as fully consumed. Silently: this is the only write path
to ``weight_used`` that left no ``spool_usage_history`` row, so the loss showed
up as nothing but a spool the page called spent with 154 g of prints behind it.
And permanently: the live sync only ever increases, so nothing walked it back.

The helper is fixed (no reading can declare a spool spent any more) and the sync
now records its corrections. This repairs the rows that were already written.

⚠️ **Restores from the usage history, which is the only honest source.** Those
rows are what the spool actually printed; the number the sync overwrote was
derived from them in the first place.

⚠️ **The predicate is the bug's fingerprint, deliberately narrow — NOT "every
spool disagrees with its history".** Plenty of spools legitimately do: one
entered as a part-used reel, one whose history was cleared, one refilled by
hand. Measured against a live farm, the broad reading would have rewritten 16
spools and ~5 kg, including two 3 kg reels carrying 2000 g and 1000 g of
correctly recorded starting usage. The narrow one selects 4, of which 3 are the
damage and the 4th is a 0.16 g float-drift no-op.

Four conditions, all required:

* **not archived** — a retired spool's number is history now, not an account
  anyone will print against (the user's call);
* **not ``weight_locked``** — the sync skips locked spools, so it cannot be the
  author of a locked spool's number. Editing ``weight_used`` by hand sets that
  flag (``inventory.py``), which is exactly how the fourth X2D slot survived;
* **``weight_used >= label_weight``** — the bad write always lands on the full
  label weight, and only real prints can add above it;
* **``weight_used`` exceeds the history sum** — otherwise there is nothing to
  give back.
"""

import logging

from sqlalchemy import func, select, update

logger = logging.getLogger(__name__)

version = 138
name = "repair_ams_sync_full_spool_writes"

# The live sync gates itself on ``new_used > current_used + 1``, so a gap under a
# gram cannot be its work — it is the float drift between a running ``+=`` total
# and history rows rounded to 0.1 g. Left alone: "repairing" it would rewrite the
# row and re-arm its low-stock warning over nothing.
_MIN_REPAIR_GRAMS = 1.0


async def upgrade(conn):
    """No DDL — this migration only repairs data."""
    return


async def seed(session_factory):
    from backend.app.models.spool import Spool
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    async with session_factory() as session:
        # ⚠️ Columns by name, never ``select(Spool)``: a later migration that
        # adds a column breaks a whole-entity select in this one, mid-chain on
        # somebody's upgrade.
        printed = dict(
            (
                await session.execute(
                    select(
                        SpoolUsageHistory.spool_id,
                        func.coalesce(func.sum(SpoolUsageHistory.weight_used), 0.0),
                    ).group_by(SpoolUsageHistory.spool_id)
                )
            ).all()
        )

        rows = (
            await session.execute(
                select(
                    Spool.id,
                    Spool.label_weight,
                    Spool.weight_used,
                    Spool.weight_used_baseline,
                    Spool.weight_locked,
                    Spool.archived_at,
                )
            )
        ).all()

        repaired = 0
        returned = 0.0
        for spool_id, label_weight, weight_used, baseline, locked, archived_at in rows:
            if archived_at is not None or locked:
                continue
            used = float(weight_used or 0.0)
            label = float(label_weight or 0.0)
            if label <= 0 or used < label:
                continue
            from_prints = round(float(printed.get(spool_id, 0.0)), 1)
            if used - from_prints < _MIN_REPAIR_GRAMS:
                continue

            values = {"weight_used": from_prints}
            if (baseline or 0) > from_prints:
                values["weight_used_baseline"] = from_prints
            # The spool just gained filament back; let it announce the next
            # run-down instead of staying muted at the level it was muted at.
            values["low_stock_notified"] = False
            await session.execute(update(Spool).where(Spool.id == spool_id).values(**values))

            repaired += 1
            returned += used - from_prints
            logger.info(
                "m138: spool %d weight_used %.1f -> %.1f (%.1f g returned; %.0f g label)",
                spool_id,
                used,
                from_prints,
                used - from_prints,
                label,
            )

        await session.commit()

    if repaired:
        logger.info("m138: repaired %d spool(s), %.1f g returned to stock", repaired, returned)
    else:
        logger.info("m138: no spool carries the signature of a zero-reading AMS sync")
