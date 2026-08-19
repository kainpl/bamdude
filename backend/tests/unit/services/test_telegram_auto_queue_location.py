"""The bot can aim work at a place, not just at a printer model.

``AutoQueueItem.target_location_id`` has gated routing to a location **subtree**
since the print dialog gained the picker — the bot was simply never given the
step, so a farm split across rooms could say "any P1S" from Telegram and nothing
more. This adds the step and threads the answer through to the row.

⚠️ The step is skipped, not shown empty, when there is nothing to choose
between: no locations at all, or none holding a printer of that model. A
question whose every answer is the same is worse than no question — in a chat it
costs a round trip and teaches the operator to tap past it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.printer_location import PrinterLocation
from backend.app.services.telegram_handlers.queue_scene import _add_to_auto_queue, _locations_for_model


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


def _callback():
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace())


async def _place(db, name: str, parent: PrinterLocation | None = None) -> PrinterLocation:
    loc = PrinterLocation(name=name, name_key=name.strip().lower(), parent_id=parent.id if parent else None)
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


class TestWhichPlacesAreWorthOffering:
    @pytest.mark.asyncio
    async def test_a_place_holding_that_model_is_offered(self, db_session, session_factory, printer_factory):
        shelf = await _place(db_session, "Shelf 1")
        p = await printer_factory(model="P1S", serial_number="LOC001")
        p.location_id = shelf.id
        await db_session.commit()

        assert await _locations_for_model("P1S") == [(shelf.id, "Shelf 1")]

    @pytest.mark.asyncio
    async def test_a_parent_is_offered_for_a_printer_on_its_shelf(self, db_session, session_factory, printer_factory):
        """The subtree rule routing itself applies — aiming at the workshop has
        to reach the printers standing on its shelves."""
        workshop = await _place(db_session, "Workshop")
        shelf = await _place(db_session, "Shelf 1", workshop)
        p = await printer_factory(model="P1S", serial_number="LOC002")
        p.location_id = shelf.id
        await db_session.commit()

        offered = dict(await _locations_for_model("P1S"))

        assert set(offered) == {workshop.id, shelf.id}
        assert offered[shelf.id] == "Workshop / Shelf 1", "the path, so two 'Shelf 1' are told apart"

    @pytest.mark.asyncio
    async def test_a_place_with_no_printer_of_that_model_is_not_offered(
        self, db_session, session_factory, printer_factory
    ):
        """Picking it would guarantee a job that waits for ever, and a chat shows
        no printer list to notice that with."""
        shelf = await _place(db_session, "Shelf 1")
        other = await _place(db_session, "Attic")
        p = await printer_factory(model="P1S", serial_number="LOC003")
        p.location_id = shelf.id
        await db_session.commit()

        assert [lid for lid, _ in await _locations_for_model("P1S")] == [shelf.id]
        assert other.id not in [lid for lid, _ in await _locations_for_model("P1S")]

    @pytest.mark.asyncio
    async def test_maintenance_mode_does_not_hide_the_place(self, db_session, session_factory, printer_factory):
        """⚠️ Deliberately weaker than the routing filter: maintenance is
        temporary, and queueing work for when the machine returns is normal."""
        shelf = await _place(db_session, "Shelf 1")
        p = await printer_factory(model="P1S", serial_number="LOC004", is_active=False)
        p.location_id = shelf.id
        await db_session.commit()

        assert [lid for lid, _ in await _locations_for_model("P1S")] == [shelf.id]

    @pytest.mark.asyncio
    async def test_an_archived_printer_does_not_keep_its_place_alive(
        self, db_session, session_factory, printer_factory
    ):
        """Retirement is permanent — that is the difference from maintenance."""
        shelf = await _place(db_session, "Shelf 1")
        p = await printer_factory(model="P1S", serial_number="LOC005")
        p.location_id = shelf.id
        p.archived = True
        await db_session.commit()

        assert await _locations_for_model("P1S") == []

    @pytest.mark.asyncio
    async def test_no_locations_at_all_offers_nothing(self, db_session, session_factory, printer_factory):
        await printer_factory(model="P1S", serial_number="LOC006")

        assert await _locations_for_model("P1S") == []


class TestTheAnswerReachesTheRow:
    @pytest.mark.asyncio
    async def test_the_chosen_place_lands_on_the_item(self, db_session, session_factory, printer_factory):
        shelf = await _place(db_session, "Shelf 1")
        lib = LibraryFile(filename="cube.3mf", file_path="library/cube.3mf", file_type="3mf", file_size=1)
        db_session.add(lib)
        await db_session.commit()

        with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
            await _add_to_auto_queue(_callback(), "en", lib.id, "P1S", None, shelf.id)

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert (item.target_model, item.target_location_id) == ("P1S", shelf.id)

    @pytest.mark.asyncio
    async def test_declining_the_step_leaves_the_item_unrestricted(self, db_session, session_factory):
        """ "Anywhere" is an answer, and it has to mean the farm — not a place."""
        lib = LibraryFile(filename="cube.3mf", file_path="library/cube.3mf", file_type="3mf", file_size=1)
        db_session.add(lib)
        await db_session.commit()

        with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
            await _add_to_auto_queue(_callback(), "en", lib.id, "P1S", None)

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.target_location_id is None
