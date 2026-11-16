"""A spool that has been used is archived, not deleted (owner decision Р6; upstream e1714b511).

A hard delete of a used spool cost the books twice:

* its usage history went with it on PostgreSQL (the forecast lost that
  consumption, a print's cost fell back to an estimate) and stayed behind as
  orphans on SQLite;
* SQLite hands the next spool ``max(id) + 1``. Delete the newest spool mid-print
  and create another: it takes the same id, and the running print — which froze
  that id in its session and its journal — is charged to a spool that was never
  in the slot. One reel, books 0 g, a print of 300 g: the NEW spool ended at
  300 g used.

So the delete is refused while anything names the spool: usage history, a slot
assignment, a running print's session, or a journal event of a print that is
still open. A spool that never printed is deleted as before.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from backend.app.models.active_print_session import ActivePrintSession
from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import usage_tracker
from backend.app.services.usage_tracker import PrintSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._active_sessions.clear()
    yield
    usage_tracker._active_sessions.clear()


@pytest.fixture(autouse=True)
def _quiet_socket():
    with patch("backend.app.core.websocket.ws_manager.broadcast", new=AsyncMock()) as broadcast:
        yield broadcast


async def _spool(db, **extra) -> Spool:
    spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=0, **extra)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _exists(db, spool_id: int) -> bool:
    return await db.scalar(select(func.count()).select_from(Spool).where(Spool.id == spool_id)) == 1


async def _delete(async_client, spool_id: int):
    return await async_client.delete(f"/api/v1/inventory/spools/{spool_id}")


async def _archive(db, printer, *, status: str) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer.id,
        filename="bracket.gcode.3mf",
        print_name="bracket",
        file_path="x/bracket.gcode.3mf",
        file_size=1,
        status=status,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


class TestASpoolNothingNames:
    async def test_is_deleted_as_before(self, async_client, db_session, _quiet_socket):
        spool = await _spool(db_session)

        response = await _delete(async_client, spool.id)

        assert response.status_code == 200, response.text
        assert await _exists(db_session, spool.id) is False
        # Hygiene the single delete lacked: other tabs learn the list changed.
        assert {"type": "inventory_changed"} in [call.args[0] for call in _quiet_socket.await_args_list]

    async def test_a_missing_spool_is_still_a_404(self, async_client):
        assert (await _delete(async_client, 987654)).status_code == 404


class TestASpoolSomethingNames:
    async def _refused(self, async_client, db_session, spool, reason: str) -> None:
        response = await _delete(async_client, spool.id)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "spool_in_use"
        assert reason in detail["reasons"]
        assert await _exists(db_session, spool.id) is True

    async def test_usage_history_keeps_it(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        db_session.add(
            SpoolUsageHistory(spool_id=spool.id, printer_id=printer.id, weight_used=42.0, status="completed")
        )
        await db_session.commit()

        await self._refused(async_client, db_session, spool, "usage_history")
        assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 1

    async def test_a_slot_assignment_keeps_it(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        await self._refused(async_client, db_session, spool, "assigned")

    async def test_a_running_prints_session_in_memory_keeps_it(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        usage_tracker._active_sessions[printer.id] = PrintSession(
            printer_id=printer.id,
            print_name="bracket",
            started_at=datetime.now(timezone.utc),
            spool_assignments={(0, 2): spool.id},
        )

        await self._refused(async_client, db_session, spool, "print_session")

    async def test_a_running_prints_saved_session_keeps_it(self, async_client, db_session, printer_factory):
        """After a restart the session is a row, not memory."""
        printer = await printer_factory()
        spool = await _spool(db_session)
        db_session.add(
            ActivePrintSession(
                printer_id=printer.id,
                print_name="bracket",
                started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                spool_assignments={"0-2": spool.id},
            )
        )
        await db_session.commit()

        await self._refused(async_client, db_session, spool, "print_session")

    async def test_a_journal_event_of_a_print_still_open_keeps_it(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        archive = await _archive(db_session, printer, status="printing")
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=0,
                event="runout",
                global_tray_id=2,
                spool_id=spool.id,
            )
        )
        await db_session.commit()

        await self._refused(async_client, db_session, spool, "open_print")

    async def test_a_journal_event_of_a_print_long_closed_does_not(self, async_client, db_session, printer_factory):
        """The journal is forensic and never pruned: a closed print's rows must not pin a spool for good."""
        printer = await printer_factory()
        spool = await _spool(db_session)
        archive = await _archive(db_session, printer, status="completed")
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=0,
                event="runout",
                global_tray_id=2,
                spool_id=spool.id,
            )
        )
        await db_session.commit()

        assert (await _delete(async_client, spool.id)).status_code == 200


class TestTheIdIsNotHandedToTheNextSpool:
    async def test_the_spool_a_running_print_froze_cannot_be_replaced_under_it(
        self, async_client, db_session, printer_factory
    ):
        """The reuse scenario itself: delete the newest spool mid-print, create another."""
        printer = await printer_factory()
        printing = await _spool(db_session)
        usage_tracker._active_sessions[printer.id] = PrintSession(
            printer_id=printer.id,
            print_name="bracket",
            started_at=datetime.now(timezone.utc),
            spool_assignments={(0, 0): printing.id},
            ams_mapping=[0],
        )

        assert (await _delete(async_client, printing.id)).status_code == 409
        newcomer = await _spool(db_session)

        # Before: the delete went through and the newcomer took the same id —
        # the print's 300 g would have been charged to it.
        assert newcomer.id != printing.id


class TestBulkDelete:
    async def test_it_deletes_what_it_may_and_names_what_it_may_not(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        unused, used, assigned = await _spool(db_session), await _spool(db_session), await _spool(db_session)
        db_session.add(SpoolUsageHistory(spool_id=used.id, printer_id=printer.id, weight_used=42.0, status="completed"))
        db_session.add(SpoolAssignment(spool_id=assigned.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        response = await async_client.post(
            "/api/v1/inventory/spools/bulk-delete", json={"spool_ids": [unused.id, used.id, assigned.id, 987654]}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted"] == 1
        assert body["not_found"] == [987654]
        assert sorted(body["refused"]) == sorted([used.id, assigned.id])
        assert await _exists(db_session, unused.id) is False
        assert await _exists(db_session, used.id) is True
        assert await _exists(db_session, assigned.id) is True
