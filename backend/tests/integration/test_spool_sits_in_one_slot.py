"""A spool sits in one slot (owner decision Р7; upstream 6a6158c94).

The tables constrain the slot, not the spool, so a second row could name the
same spool: a hand "move" left both, and a reel pre-assigned on one printer and
loaded into another sat on both. A print from the vacated slot was then charged
to a spool that is not in it.

Every assignment writer now takes the spool out of its other slots. What must
NOT move with it — and is pinned here with figures — is the accounting: a
vacated slot is not a loaded one (no journal row), a slot a running print feeds
from is never vacated (409 for a hand move), and a print is charged exactly the
grams and the history rows it was charged before.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from backend.app import main as main_module
from backend.app.models.active_print_session import ActivePrintSession
from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import spool_single_slot, usage_tracker
from backend.app.services.print_file_analysis import PrintFileAnalysis
from backend.app.services.usage_tracker import PrintSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    spool_single_slot._pending_elsewhere.clear()
    spool_single_slot._tags.clear()
    yield
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    spool_single_slot._pending_elsewhere.clear()
    spool_single_slot._tags.clear()


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)
    monkeypatch.setattr("backend.app.core.database.async_session", _session_ctx)


async def _spool(db, **extra) -> Spool:
    spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=200, cost_per_kg=20.0, **extra)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _row(db, spool, printer, ams_id: int, tray_id: int, *, age_minutes: int = 0) -> None:
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=ams_id,
            tray_id=tray_id,
            fingerprint_color="FF0000FF",
            fingerprint_type="PLA",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_minutes),
        )
    )
    await db.commit()


async def _slots_of(db, spool) -> list[tuple[int, int, int]]:
    rows = (
        await db.execute(
            select(SpoolAssignment.printer_id, SpoolAssignment.ams_id, SpoolAssignment.tray_id)
            .where(SpoolAssignment.spool_id == spool.id)
            .order_by(SpoolAssignment.printer_id, SpoolAssignment.ams_id, SpoolAssignment.tray_id)
        )
    ).all()
    return [tuple(row) for row in rows]


def _status(state: str = "IDLE", **extra) -> SimpleNamespace:
    fields = {"raw_data": {}, "connected": True, "tray_now": 255, "last_loaded_tray": -1, "tray_change_log": []}
    return SimpleNamespace(state=state, **{**fields, **extra})


@asynccontextmanager
async def _printers(statuses: dict[int, SimpleNamespace]):
    """The printer manager as the assign route and the module see it: one status per printer id."""
    broadcast = AsyncMock()
    with (
        patch("backend.app.services.printer_manager.printer_manager.get_status", side_effect=statuses.get),
        patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=None),
        patch(
            "backend.app.services.printer_manager.printer_manager.ensure_fresh_connection",
            new=AsyncMock(return_value=True),
        ),
        patch("backend.app.core.websocket.ws_manager.broadcast", new=broadcast),
    ):
        yield broadcast


async def _assign(async_client, spool, printer, ams_id: int, tray_id: int):
    return await async_client.post(
        "/api/v1/inventory/assignments",
        json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": ams_id, "tray_id": tray_id},
    )


def _session(printer_id: int, spool_id: int, mapping: list[int]) -> PrintSession:
    return PrintSession(
        printer_id=printer_id,
        print_name="bracket",
        started_at=datetime.now(timezone.utc),
        tray_remain_start={(0, 0): 80},
        tray_now_at_start=mapping[0],
        spool_assignments={(0, mapping[0]): spool_id},
        ams_mapping=mapping,
    )


class TestAHandMove:
    async def test_within_one_printer_the_old_slot_is_vacated(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)

        async with _printers({printer.id: _status()}) as broadcast:
            response = await _assign(async_client, spool, printer, 0, 2)

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 2)]
        # A vacated slot is not a loaded one: nothing is journalled for it.
        assert await db_session.scalar(select(func.count()).select_from(PrintUsageEvent)) == 0
        announced = [(c.args[0]["ams_id"], c.args[0]["tray_id"]) for c in broadcast.await_args_list]
        assert (0, 0) in announced and (0, 2) in announced

    async def test_to_another_printer_the_old_printers_slot_is_vacated(self, async_client, db_session, printer_factory):
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        spool = await _spool(db_session)
        await _row(db_session, spool, first, 0, 0)

        async with _printers({first.id: _status(), second.id: _status()}):
            response = await _assign(async_client, spool, second, 0, 1)

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, spool) == [(second.id, 0, 1)]

    async def test_a_reassign_to_the_same_slot_changes_nothing_else(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool, other = await _spool(db_session), await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)
        await _row(db_session, other, printer, 0, 1)

        async with _printers({printer.id: _status()}):
            response = await _assign(async_client, spool, printer, 0, 0)

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 0)]
        assert await _slots_of(db_session, other) == [(printer.id, 0, 1)]

    async def test_it_is_refused_while_a_print_feeds_from_the_old_slot(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0, last_loaded_tray=0)}):
            response = await _assign(async_client, spool, printer, 0, 2)

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "spool_feeds_print"
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 0)]

    async def test_a_print_whose_trays_are_unknown_refuses_every_slot_of_its_printer(
        self, async_client, db_session, printer_factory
    ):
        """A restart restored no session: the print may be feeding from any slot."""
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 3)

        async with _printers({printer.id: _status("RUNNING")}):
            response = await _assign(async_client, spool, printer, 0, 2)

        assert response.status_code == 409, response.text
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 3)]

    async def test_a_slot_the_print_does_not_feed_from_may_be_vacated_mid_print(
        self, async_client, db_session, printer_factory
    ):
        printer = await printer_factory()
        printing, moved = await _spool(db_session), await _spool(db_session)
        await _row(db_session, printing, printer, 0, 0)
        await _row(db_session, moved, printer, 0, 1)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, printing.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0, last_loaded_tray=0)}):
            response = await _assign(async_client, moved, printer, 0, 2)

        assert response.status_code == 200, response.text
        assert await _slots_of(db_session, moved) == [(printer.id, 0, 2)]
        assert await _slots_of(db_session, printing) == [(printer.id, 0, 0)]
        # The running print's own records are not this rule's: nothing was written for the vacated slot.
        events = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
        assert [e for e in events if e.global_tray_id == 1] == []
        assert await db_session.scalar(select(func.count()).select_from(ActivePrintSession)) == 0


ANALYSIS = PrintFileAnalysis(
    filament_usage=[{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}],
    layer_usage=None,
    filament_properties={},
)


async def _book_a_300g_print(db, printer, spool) -> tuple[float, list[tuple[str, float]]]:
    """The tracker's own completion of a 300 g print from AMS0-T0; returns the spool's books."""
    archive = PrintArchive(
        printer_id=printer.id,
        filename="bracket.gcode.3mf",
        print_name="bracket",
        file_path="x/bracket.gcode.3mf",
        file_size=1,
        status="completed",
        filament_used_grams=300.0,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    status = SimpleNamespace(
        state="FINISH",
        raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 50}]}]},
        tray_now=255,
        last_loaded_tray=0,
        tray_change_log=[(0, 0)],
        connected=True,
    )
    await usage_tracker.on_print_complete(
        printer.id,
        {"status": "completed"},
        SimpleNamespace(get_status=lambda _pid: status),
        db,
        archive_id=archive.id,
        ams_mapping=[0],
        file_analysis=ANALYSIS,
        analysis_attempted=True,
    )
    await db.refresh(spool)
    rows = (
        (await db.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool.id).order_by("id")))
        .scalars()
        .all()
    )
    return round(spool.weight_used, 1), [(r.status, round(r.weight_used, 1)) for r in rows]


class TestAccountingIsNotMoved:
    """GUARD 1: one reel, books 200 g, a print of 300 g from AMS0-T0. Truth: 500."""

    async def test_a_normal_print_is_charged_exactly_as_before(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, [0])

        assert await _book_a_300g_print(db_session, printer, spool) == (500.0, [("completed", 300.0)])

    async def test_a_print_is_charged_the_same_when_another_spool_was_moved_meanwhile(
        self, async_client, db_session, printer_factory
    ):
        printer = await printer_factory()
        printing, moved = await _spool(db_session), await _spool(db_session)
        await _row(db_session, printing, printer, 0, 0)
        await _row(db_session, moved, printer, 0, 1)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, printing.id, [0])

        async with _printers({printer.id: _status("RUNNING", tray_now=0, last_loaded_tray=0)}):
            assert (await _assign(async_client, moved, printer, 0, 2)).status_code == 200

        assert await _book_a_300g_print(db_session, printer, printing) == (500.0, [("completed", 300.0)])
        await db_session.refresh(moved)
        assert moved.weight_used == 200  # the moved spool was charged nothing

    async def test_a_print_is_charged_by_its_frozen_spool_even_if_its_row_were_gone(self, db_session, printer_factory):
        """Why vacating a row is safe for the books at all: the session froze the spool id."""
        printer = await printer_factory()
        spool = await _spool(db_session)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, [0])  # no row at all

        assert await _book_a_300g_print(db_session, printer, spool) == (500.0, [("completed", 300.0)])


def _ams(tray_id: int) -> list:
    tray = {
        "id": tray_id,
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tray_uuid": UUID,
        "tag_uid": "0102030405060708",
        "remain": 80,
    }
    return [{"id": 0, "tray": [tray]}]


async def _tag_read(printer_id: int, tray_id: int, statuses: dict[int, SimpleNamespace]) -> None:
    """One AMS push through the real callback: the reel's tag is read in ``tray_id``."""
    ams = _ams(tray_id)
    statuses[printer_id].raw_data = {"ams": ams}
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
        patch.object(main_module.printer_manager, "get_status", side_effect=statuses.get),
        patch.object(main_module.printer_manager, "get_client", return_value=None),
        patch.object(main_module.printer_manager, "ensure_fresh_connection", new=AsyncMock(return_value=True)),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, ams)


class TestATagRead:
    async def test_on_another_printer_it_takes_the_spool_out_of_the_first(self, db_session, main_db, printer_factory):
        """Upstream's own report: pre-assigned on printer A, the reel is loaded into printer B."""
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        spool = await _spool(db_session, tray_uuid=UUID)
        await _row(db_session, spool, first, 0, 0)

        await _tag_read(second.id, 1, {first.id: _status(), second.id: _status()})

        assert await _slots_of(db_session, spool) == [(second.id, 0, 1)]

    async def test_a_slot_feeding_a_running_print_keeps_its_row(self, db_session, main_db, printer_factory):
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        spool = await _spool(db_session, tray_uuid=UUID)
        await _row(db_session, spool, first, 0, 0)
        usage_tracker._active_sessions[first.id] = _session(first.id, spool.id, [0])

        await _tag_read(
            second.id, 1, {first.id: _status("RUNNING", tray_now=0, last_loaded_tray=0), second.id: _status()}
        )

        assert await _slots_of(db_session, spool) == [(first.id, 0, 0), (second.id, 0, 1)]
        assert await db_session.scalar(select(func.count()).select_from(PrintUsageEvent)) == 0


class TestRowsThatWereAlreadyDouble:
    """Owner decision Р36: the slot that reports the tag keeps the spool, else the newest row."""

    async def test_an_untagged_spool_keeps_its_newest_row(self, db_session, main_db, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0, age_minutes=60)
        await _row(db_session, spool, printer, 0, 2, age_minutes=1)

        with patch(
            "backend.app.services.printer_manager.printer_manager.get_status", side_effect={printer.id: _status()}.get
        ):
            released = await spool_single_slot.resolve_duplicates(db_session, printer.id)
            await db_session.commit()

        assert released.vacated == [(printer.id, 0, 0)]
        assert await _slots_of(db_session, spool) == [(printer.id, 0, 2)]

    async def test_a_tagged_spool_keeps_the_slot_that_reports_its_tag(self, db_session, main_db, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session, tray_uuid=UUID)
        await _row(db_session, spool, printer, 0, 0, age_minutes=60)  # the older row, and the one the tag is in
        await _row(db_session, spool, printer, 0, 2, age_minutes=1)
        status = _status()
        status.raw_data = {"ams": _ams(0)}

        with patch(
            "backend.app.services.printer_manager.printer_manager.get_status", side_effect={printer.id: status}.get
        ):
            await spool_single_slot.resolve_duplicates(db_session, printer.id)
            await db_session.commit()

        assert await _slots_of(db_session, spool) == [(printer.id, 0, 0)]

    async def test_a_spool_in_one_slot_is_left_alone(self, db_session, main_db, printer_factory):
        printer = await printer_factory()
        spool = await _spool(db_session)
        await _row(db_session, spool, printer, 0, 0)

        with patch(
            "backend.app.services.printer_manager.printer_manager.get_status", side_effect={printer.id: _status()}.get
        ):
            released = await spool_single_slot.resolve_duplicates(db_session, printer.id)

        assert (released.vacated, released.deferred) == ([], [])


class TestSpoolmanLedger:
    async def _ledger(self, db, printer, ams_id: int, tray_id: int, spool_id: int) -> None:
        db.add(
            SpoolmanSlotAssignment(printer_id=printer.id, ams_id=ams_id, tray_id=tray_id, spoolman_spool_id=spool_id)
        )
        await db.commit()

    async def _slots(self, db, spool_id: int) -> list[tuple[int, int, int]]:
        rows = (
            await db.execute(
                select(SpoolmanSlotAssignment.printer_id, SpoolmanSlotAssignment.ams_id, SpoolmanSlotAssignment.tray_id)
                .where(SpoolmanSlotAssignment.spoolman_spool_id == spool_id)
                .order_by(SpoolmanSlotAssignment.printer_id, SpoolmanSlotAssignment.tray_id)
            )
        ).all()
        return [tuple(row) for row in rows]

    async def test_the_other_rows_of_a_spoolman_spool_are_removed(self, db_session, printer_factory):
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        await self._ledger(db_session, first, 0, 0, 42)
        await self._ledger(db_session, second, 0, 1, 42)
        await self._ledger(db_session, first, 0, 3, 7)  # another spool's row is nobody's business

        with patch(
            "backend.app.services.printer_manager.printer_manager.get_status",
            side_effect={first.id: _status(), second.id: _status()}.get,
        ):
            released = await spool_single_slot.release_other_spoolman_slots(
                db_session, spoolman_spool_id=42, keep=(second.id, 0, 1), refuse=True
            )
            await db_session.commit()

        assert released.vacated == [(first.id, 0, 0)]
        assert await self._slots(db_session, 42) == [(second.id, 0, 1)]
        assert await self._slots(db_session, 7) == [(first.id, 0, 3)]

    async def test_a_hand_move_off_a_printing_printer_is_refused(self, db_session, printer_factory):
        """Spoolman mode registers no session, so a printing printer's trays are unknown: every slot counts."""
        first = await printer_factory(name="A", serial_number="SERIAL-A")
        second = await printer_factory(name="B", serial_number="SERIAL-B")
        await self._ledger(db_session, first, 0, 0, 42)

        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.get_status",
                side_effect={first.id: _status("RUNNING"), second.id: _status()}.get,
            ),
            pytest.raises(spool_single_slot.SpoolFeedsPrint),
        ):
            await spool_single_slot.release_other_spoolman_slots(
                db_session, spoolman_spool_id=42, keep=(second.id, 0, 1), refuse=True
            )

        assert await self._slots(db_session, 42) == [(first.id, 0, 0)]
