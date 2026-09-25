"""Remove imported AMS slot markers from the storage-location catalogue (upstream 537b4d25).

Older versions recorded the slot a spool was loaded into by writing
"<printer> - AMS A1" into Spoolman's ``location`` field. The strings stayed on
people's Spoolman spools, and ``sync_locations_from_spoolman`` imported every
distinct one — so a printer slot appeared in the Storage Location dropdown as a
place to put a spool away, and could not be removed by hand: the delete route
refuses a location with spools, and in Spoolman mode counts them by that same
string. The sync now skips them (``location_service.is_ams_slot_location``);
this clears the ones already in the catalogue.

A row is removed only when no spool in this database points at it — neither by
``location_id`` nor by the free-text ``storage_location`` (compared through
``location_name_key``, in Python, so the database's case folding does not
decide) — so a spool deliberately filed under such a name keeps it. Spools in
Spoolman are not consulted and not touched: their location strings are the
user's data on the user's server.
"""

from __future__ import annotations

import logging

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

version = 185
name = "drop_ams_slot_locations"


async def upgrade(conn):
    """No schema change — the cleanup is data, in ``seed``."""


async def seed(session_factory):
    from backend.app.services.location_service import is_ams_slot_location, location_name_key

    async with session_factory() as db:
        markers = [
            row
            for row in (await db.execute(text("SELECT id, name FROM locations"))).all()
            if is_ams_slot_location(row.name)
        ]
        if not markers:
            return
        held_ids = set(
            (await db.execute(text("SELECT DISTINCT location_id FROM spool WHERE location_id IS NOT NULL"))).scalars()
        )
        held_keys = {
            location_name_key(value)
            for value in (
                await db.execute(text("SELECT DISTINCT storage_location FROM spool WHERE storage_location IS NOT NULL"))
            ).scalars()
            if value and value.strip()
        }
        doomed = [row for row in markers if row.id not in held_ids and location_name_key(row.name) not in held_keys]
        if not doomed:
            return
        await db.execute(
            text("DELETE FROM locations WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": [row.id for row in doomed]},
        )
        await db.commit()
        logger.info(
            "m185: removed %d AMS slot marker(s) from the storage-location catalogue: %s",
            len(doomed),
            ", ".join(sorted(row.name for row in doomed)),
        )
