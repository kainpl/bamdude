"""AMS slot markers are not storage locations (upstream 537b4d25).

An older Bambuddy / BamDude wrote the slot a spool was loaded into —
"<printer> - AMS A1" — into Spoolman's ``location`` field. The strings survive on
people's Spoolman spools, the location sync imported every distinct one, and a
printer slot was offered as somewhere to put a spool away. The sync now skips
them, and m185 clears the ones already imported that no spool here points at.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text

from backend.app.models.location import Location
from backend.app.models.spool import Spool
from backend.app.services.location_service import (
    assign_location_name,
    is_ams_slot_location,
    sync_locations_from_spoolman,
)


@pytest.mark.parametrize(
    "name",
    [
        "AMS A1",
        "AMS D4",
        "AMS-HT A1",
        "AMS HT B1",
        "External Spool",
        "H2D-1 - AMS A1",
        "Farm X1C - AMS-HT A1",
        "P1S - External Spool",
        "  ams b2  ",
    ],
)
def test_the_slot_marker_shapes_are_recognised(name):
    assert is_ams_slot_location(name)


@pytest.mark.parametrize(
    "name",
    ["AMS Drybox", "Spare AMS trays", "Shelf A1", "AMS", "Drybox - AMS", "External shelf", "AMS A", "Rack 2"],
)
def test_a_real_shelf_is_left_alone(name):
    """Narrow on purpose: anything the filter swallowed is a place nobody could file a spool under."""
    assert not is_ams_slot_location(name)


@pytest.mark.asyncio
async def test_the_spoolman_sync_skips_slot_markers(db_session):
    client = MagicMock()
    client.get_distinct_locations = AsyncMock(return_value=["Shelf 1", "H2D-1 - AMS A1", "External Spool"])

    await sync_locations_from_spoolman(db_session, client)
    await db_session.commit()

    names = set((await db_session.execute(select(Location.name))).scalars().all())
    assert names == {"Shelf 1"}


async def _location(db, name: str) -> Location:
    location = Location()
    assign_location_name(location, name)
    db.add(location)
    await db.commit()
    await db.refresh(location)
    return location


@pytest.mark.asyncio
async def test_m185_drops_imported_markers_nobody_points_at(db_session, test_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.migrations import m185_drop_ams_slot_locations as m185

    shelf = await _location(db_session, "Shelf 1")
    await _location(db_session, "H2D-1 - AMS A1")
    held_by_id = await _location(db_session, "AMS B2")
    await _location(db_session, "External Spool")
    held_by_name = await _location(db_session, "P1S - AMS C3")
    db_session.add(Spool(material="PLA", label_weight=1000, core_weight=250, weight_used=0, location_id=held_by_id.id))
    db_session.add(
        Spool(material="PLA", label_weight=1000, core_weight=250, weight_used=0, storage_location="p1s - ams c3")
    )
    await db_session.commit()

    await m185.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    names = set((await db_session.execute(text("SELECT name FROM locations"))).scalars().all())
    assert names == {shelf.name, held_by_id.name, held_by_name.name}
