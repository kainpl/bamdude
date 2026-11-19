"""Archiving a spool takes it out of its slot (upstream 23d04c864, adapted).

``archive_spool`` only stamped ``archived_at``. The slot row stayed: the next
print from that slot was charged to the archived spool, the printer card kept
showing it, and nothing could remove the row — delete answers "archive it
instead" for an assigned spool and the assign picker refuses an archived one.

The row now goes through the single-slot rule's writer, and the same rule holds
as for a move: a slot a running print feeds from is never vacated, so that spool
is not archived (409; in a bulk request it is skipped and reported).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.api.routes import spoolman_inventory
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import ams_advertised_overlay, usage_tracker
from backend.tests.integration.test_spool_sits_in_one_slot import (  # noqa: F401
    _clean_memory,
    _printers,
    _row,
    _session,
    _slots_of,
    _spool,
    _status,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _announced(broadcast) -> list[tuple]:
    return [
        (call.args[0]["printer_id"], call.args[0]["ams_id"], call.args[0]["tray_id"])
        for call in broadcast.await_args_list
        if call.args[0].get("type") == "spool_assignment_changed"
    ]


def _types(broadcast) -> list[str]:
    return [call.args[0].get("type") for call in broadcast.await_args_list]


class TestBuiltInInventory:
    async def test_the_archived_spool_leaves_its_slot_and_the_card_is_told(
        self, async_client, db_session, printer_factory, monkeypatch
    ):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 1)
        forgotten = MagicMock()
        monkeypatch.setattr(ams_advertised_overlay, "forget", forgotten)

        async with _printers({printer.id: _status()}) as broadcast:
            response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/archive")

        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None
        assert await _slots_of(db_session, spool) == []
        assert _announced(broadcast) == [(printer.id, 0, 1)]
        assert "inventory_changed" in _types(broadcast)
        forgotten.assert_called_once_with(printer.id, 0, 1)

    async def test_rows_on_two_printers_both_go(self, async_client, db_session, printer_factory):
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        spool = await _spool(db_session)
        await _row(db_session, spool, first, 0, 0)
        await _row(db_session, spool, second, 128, 0)

        async with _printers({first.id: _status(), second.id: _status()}):
            response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/archive")

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, spool) == []

    async def test_a_spool_feeding_a_running_print_is_not_archived(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0)}) as broadcast:
            response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/archive")

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "spool_feeds_print"
        await db_session.refresh(spool)
        assert spool.archived_at is None
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 0)]
        assert _announced(broadcast) == []

    async def test_a_slot_the_running_print_does_not_use_is_no_obstacle(
        self, async_client, db_session, printer_factory
    ):
        printer = await printer_factory()
        feeding, idle = await _spool(db_session), await _spool(db_session)
        await _row(db_session, feeding, printer, 0, 0)
        await _row(db_session, idle, printer, 0, 3)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, feeding.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0)}):
            response = await async_client.post(f"/api/v1/inventory/spools/{idle.id}/archive")

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, idle) == []
        assert await _slots_of(db_session, feeding) == [(printer.id, 0, 0)]

    async def test_a_spool_in_no_slot_is_archived_as_before(self, async_client, db_session):
        spool = await _spool(db_session)

        async with _printers({}) as broadcast:
            response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/archive")

        assert response.status_code == 200, response.text
        assert _announced(broadcast) == []

    async def test_bulk_skips_and_reports_the_feeding_spool(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        feeding, idle, loose = await _spool(db_session), await _spool(db_session), await _spool(db_session)
        await _row(db_session, feeding, printer, 0, 0)
        await _row(db_session, idle, printer, 0, 2)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, feeding.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0)}) as broadcast:
            response = await async_client.post(
                "/api/v1/inventory/spools/bulk-archive", json={"spool_ids": [feeding.id, idle.id, loose.id]}
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["archived"], body["refused"]) == (2, [feeding.id])
        for spool in (feeding, idle, loose):
            await db_session.refresh(spool)
        assert feeding.archived_at is None
        assert idle.archived_at is not None and loose.archived_at is not None
        assert await _slots_of(db_session, feeding) == [(printer.id, 0, 0)]
        assert await _slots_of(db_session, idle) == []
        assert _announced(broadcast) == [(printer.id, 0, 2)]


class TestSpoolmanInventory:
    @pytest.fixture
    def spoolman(self, monkeypatch):
        client = MagicMock()
        client.set_spool_archived = AsyncMock(return_value={"id": 42, "archived": True})
        client.delete_spool = AsyncMock(return_value=None)
        monkeypatch.setattr(spoolman_inventory, "_get_client", AsyncMock(return_value=client))
        monkeypatch.setattr(spoolman_inventory, "_map_spoolman_spool", lambda spool: spool)
        return client

    @staticmethod
    async def _ledger(db, printer, ams_id: int, tray_id: int, spool_id: int = 42) -> None:
        db.add(
            SpoolmanSlotAssignment(printer_id=printer.id, ams_id=ams_id, tray_id=tray_id, spoolman_spool_id=spool_id)
        )
        await db.commit()

    @staticmethod
    async def _rows(db, spool_id: int = 42) -> list[tuple]:
        rows = await db.execute(
            select(
                SpoolmanSlotAssignment.printer_id, SpoolmanSlotAssignment.ams_id, SpoolmanSlotAssignment.tray_id
            ).where(SpoolmanSlotAssignment.spoolman_spool_id == spool_id)
        )
        return [tuple(row) for row in rows.all()]

    @pytest.mark.parametrize("door", ["archive", "delete"])
    async def test_the_spool_leaves_its_slot(self, db_session, printer_factory, spoolman, door):
        printer = await printer_factory()
        await self._ledger(db_session, printer, 0, 1)
        route = spoolman_inventory.archive_spool if door == "archive" else spoolman_inventory.delete_spool

        async with _printers({printer.id: _status()}) as broadcast:
            await route(spool_id=42, db=db_session, _=None)

        assert await self._rows(db_session) == []
        assert _announced(broadcast) == [(printer.id, 0, 1)]

    @pytest.mark.parametrize("door", ["archive", "delete"])
    async def test_a_printing_printer_keeps_it_and_spoolman_is_not_touched(
        self, db_session, printer_factory, spoolman, door
    ):
        """Spoolman mode registers no session, so every slot of a printing printer counts."""
        printer = await printer_factory()
        await self._ledger(db_session, printer, 0, 1)
        route = spoolman_inventory.archive_spool if door == "archive" else spoolman_inventory.delete_spool

        async with _printers({printer.id: _status("RUNNING")}):
            with pytest.raises(HTTPException) as refused:
                await route(spool_id=42, db=db_session, _=None)

        assert refused.value.status_code == 409
        assert refused.value.detail["error"] == "spool_feeds_print"
        spoolman.set_spool_archived.assert_not_awaited()
        spoolman.delete_spool.assert_not_awaited()
        assert await self._rows(db_session) == [(printer.id, 0, 1)]

    async def test_bulk_reports_the_refusal_as_its_sentence(self, db_session, printer_factory, spoolman):
        printer = await printer_factory()
        await self._ledger(db_session, printer, 0, 1)
        data = spoolman_inventory.SpoolBulkIds(spool_ids=[42, 43])

        async with _printers({printer.id: _status("RUNNING")}):
            result = await spoolman_inventory.bulk_archive_spools(data=data, db=db_session, user=None)

        assert result["archived"] == 1
        assert [(e["id"], e["status"]) for e in result["errors"]] == [(42, 409)]
        assert result["errors"][0]["detail"].startswith("This spool is in a slot a running print feeds from")


class TestSpoolsArchivedBeforeTheRule:
    """A spool archived before archiving vacated its slots still holds its row; the
    row goes as the printer reports in — only while it reports that it is not printing."""

    async def _archived_in_slot(self, db_session, printer, ams_id=0, tray_id=1):
        from datetime import datetime, timezone

        spool = await _spool(db_session, archived_at=datetime.now(timezone.utc))
        await _row(db_session, spool, printer, ams_id, tray_id)
        return spool

    async def test_an_idle_printer_gives_the_row_up(self, db_session, printer_factory):
        from backend.app.services import spool_single_slot

        printer = await printer_factory()
        archived = await self._archived_in_slot(db_session, printer)
        active = await _spool(db_session)
        await _row(db_session, active, printer, 0, 2)

        async with _printers({printer.id: _status("IDLE")}):
            released = await spool_single_slot.release_archived(db_session, printer.id)
            await db_session.commit()

        assert released.vacated == [(printer.id, 0, 1)]
        assert await _slots_of(db_session, archived) == []
        assert await _slots_of(db_session, active) == [(printer.id, 0, 2)], "an active spool is not touched"

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", ""])
    async def test_a_printing_or_unreported_printer_keeps_it(self, db_session, printer_factory, state):
        from backend.app.services import spool_single_slot

        printer = await printer_factory()
        archived = await self._archived_in_slot(db_session, printer)

        async with _printers({printer.id: _status(state)}):
            released = await spool_single_slot.release_archived(db_session, printer.id)
            await db_session.commit()

        assert released.vacated == [] and released.feeding == [(printer.id, 0, 1)]
        assert await _slots_of(db_session, archived) == [(printer.id, 0, 1)]

    async def test_a_disconnected_printer_or_a_print_still_to_be_booked_keeps_it(self, db_session, printer_factory):
        from backend.app.services import spool_single_slot

        printer = await printer_factory()
        archived = await self._archived_in_slot(db_session, printer)

        async with _printers({printer.id: _status("IDLE", connected=False)}):
            assert (await spool_single_slot.release_archived(db_session, printer.id)).vacated == []
        usage_tracker._completing[printer.id] = object()
        async with _printers({printer.id: _status("FINISH")}):
            assert (await spool_single_slot.release_archived(db_session, printer.id)).vacated == []
        async with _printers({}):
            assert (await spool_single_slot.release_archived(db_session, printer.id)).vacated == []

        assert await _slots_of(db_session, archived) == [(printer.id, 0, 1)]

    async def test_another_printers_rows_are_left_to_that_printer(self, db_session, printer_factory):
        from backend.app.services import spool_single_slot

        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        archived = await self._archived_in_slot(db_session, first)
        await _row(db_session, archived, second, 0, 0)

        async with _printers({first.id: _status("IDLE"), second.id: _status("IDLE")}):
            await spool_single_slot.release_archived(db_session, first.id)
            await db_session.commit()

        assert await _slots_of(db_session, archived) == [(second.id, 0, 0)]
