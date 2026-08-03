"""Routing by identity, not by spelling.

The behaviour is unchanged — a printer in the target place is eligible, one
elsewhere is not. What changes is that the comparison can no longer be defeated
by capitalisation, and that renaming a place does not silently unroute the work
already waiting for it.

Before this, ``Printer.location == item.target_location`` compared strings
exactly, so an item aimed at a mistyped place matched nothing — silently and
for ever, because "no printer matches" is a legitimate state for the dispatcher.
"""

import pytest

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_eligibility import find_eligible_printer

# What the location filter says when it excluded every candidate. Asserting on
# this rather than on a printer being returned keeps these tests about the
# filter: a printer that passes it still has to be online, plate-clear and
# stocked, and those gates are somebody else's subject.
_EXCLUDED_BY_LOCATION = "No active"


async def _place(db, name: str) -> PrinterLocation:
    row = PrinterLocation(name=name, name_key=name.strip().lower())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_in_the_target_location_is_eligible(db_session, printer_factory):
    place = await _place(db_session, "Shop 2")
    p = await printer_factory(name="X1", serial_number="LOC01", model="X1C", is_active=True)
    p.location_id = place.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=place.id), set()
    )

    assert _EXCLUDED_BY_LOCATION not in (reason or ""), "the place must not exclude it"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_elsewhere_is_not(db_session, printer_factory):
    here = await _place(db_session, "Shop 1")
    there = await _place(db_session, "Shop 2")
    p = await printer_factory(name="X1", serial_number="LOC02", model="X1C", is_active=True)
    p.location_id = here.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=there.id), set()
    )

    assert printer is None
    assert _EXCLUDED_BY_LOCATION in reason


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_item_with_no_target_place_takes_any_printer(db_session, printer_factory):
    place = await _place(db_session, "Shop 2")
    p = await printer_factory(name="X1", serial_number="LOC03", model="X1C", is_active=True)
    p.location_id = place.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    _printer, reason = await find_eligible_printer(db_session, AutoQueueItem(target_model="X1C"), set())

    assert _EXCLUDED_BY_LOCATION not in (reason or "")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_renaming_the_place_does_not_change_where_work_goes(db_session, printer_factory):
    """The new capability, and the one a later 'simplification' that resolves
    names instead of ids would silently destroy.

    Under the string scheme this rename unrouted the item: the printer's
    location string changed and the item's target did not.
    """
    place = await _place(db_session, "Shop 2")
    p = await printer_factory(name="X1", serial_number="LOC04", model="X1C", is_active=True)
    p.location_id = place.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    place.name, place.name_key = "Workshop", "workshop"
    await db_session.commit()

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=place.id), set()
    )

    assert _EXCLUDED_BY_LOCATION not in (reason or ""), "the rename must not unroute it"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_waiting_reason_names_the_place_a_human_would_recognise(db_session, printer_factory):
    """The reason string is read by an operator wondering why nothing moved. An
    id in it answers nothing."""
    here = await _place(db_session, "Shop 1")
    there = await _place(db_session, "Цех 2")
    p = await printer_factory(name="X1", serial_number="LOC05", model="X1C", is_active=True)
    p.location_id = here.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=there.id), set()
    )

    assert "Цех 2" in reason


@pytest.mark.asyncio
@pytest.mark.integration
async def test_work_aimed_at_a_workshop_reaches_a_printer_on_a_shelf(db_session, printer_factory):
    """The point of the tree. Before this the item had to name each shelf, and
    aiming at the room they are in matched nothing at all."""
    workshop = await _place(db_session, "Workshop")
    shelf = PrinterLocation(name="Shelf", name_key="shelf", parent_id=workshop.id)
    db_session.add(shelf)
    await db_session.commit()
    await db_session.refresh(shelf)

    p = await printer_factory(name="X1", serial_number="LOC10", model="X1C", is_active=True)
    p.location_id = shelf.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=workshop.id), set()
    )

    assert _EXCLUDED_BY_LOCATION not in (reason or ""), "a shelf is inside the workshop"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_in_a_sibling_workshop_is_still_excluded(db_session, printer_factory):
    """The subtree is a gate, not an amnesty: widening it to the whole tree
    would make the target meaningless."""
    workshop = await _place(db_session, "Workshop")
    other = await _place(db_session, "Hall")
    shelf = PrinterLocation(name="Shelf", name_key="shelf", parent_id=other.id)
    db_session.add(shelf)
    await db_session.commit()
    await db_session.refresh(shelf)

    p = await printer_factory(name="X1", serial_number="LOC11", model="X1C", is_active=True)
    p.location_id = shelf.id
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=workshop.id), set()
    )

    assert _EXCLUDED_BY_LOCATION in (reason or "")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_refusal_names_the_path_not_the_bare_name(db_session):
    """ "No printers in Shelf" reads oddly when the operator chose a workshop."""
    workshop = await _place(db_session, "Workshop")
    shelf = PrinterLocation(name="Shelf", name_key="shelf", parent_id=workshop.id)
    db_session.add(shelf)
    await db_session.commit()
    await db_session.refresh(shelf)

    _printer, reason = await find_eligible_printer(
        db_session, AutoQueueItem(target_model="X1C", target_location_id=shelf.id), set()
    )

    assert "Workshop / Shelf" in (reason or "")
