"""The remain% weight sync never writes while a print is unbooked (upstream 5f13d5770, 5afb485c1, 0b1d6c238).

The AMS reports what is left on an RFID reel in whole percents. While nothing prints, BamDude moves the
books to that reading. While a print is running or being closed out, the print's own filament is still to
be charged from the 3MF -- a sync in that time writes the same filament once as "the AMS says so" and the
tracker adds it a second time.

One reel in every scenario: 1 kg, books ``weight_used = 200`` (AMS remain 80 %), a print of 300 g. Truth
after the print: 500.

- restart mid-print that restored no session, AMS push at 50 % of the print: 650 before the fix;
- AMS push between the session leaving memory and the booking's commit: 800;
- the Sync button pressed mid-print: 650 (Spoolman: 350 g left instead of 500);
- Spoolman, a tagged reel, AMS push between FINISH and the usage report: 300 g used twice.

What the tracker charges is not this module's subject and must not move: the last test of each group books
the same print with and without the guard in play and compares the rows.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main as main_module
from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import usage_tracker
from backend.app.services.print_file_analysis import PrintFileAnalysis
from backend.app.services.spoolman import SpoolmanClient
from backend.app.services.usage_tracker import PrintSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    yield
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _reel(db, printer_factory):
    printer = await printer_factory()
    spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=200, tray_uuid=UUID, cost_per_kg=20.0)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
            fingerprint_color="FF0000FF",
            fingerprint_type="PLA",
        )
    )
    await db.commit()
    return printer, spool


def _ams(remain: int) -> list:
    tray = {
        "id": 0,
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tray_uuid": UUID,
        "tag_uid": "0102030405060708",
        "remain": remain,
    }
    return [{"id": 0, "tray": [tray]}]


async def _push(printer_id: int, remain: int, state: str) -> None:
    """One AMS push through the real callback."""
    status = SimpleNamespace(state=state, raw_data={}, connected=True)
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
        patch.object(main_module.printer_manager, "get_status", return_value=status),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, _ams(remain))


async def _archive(db, printer_id: int) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
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
    return archive


ANALYSIS = PrintFileAnalysis(
    filament_usage=[{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}],
    layer_usage=None,
    filament_properties={},
)


def _printer_manager(remain: int, state: str = "FINISH"):
    status = SimpleNamespace(
        state=state,
        raw_data={"ams": _ams(remain)},
        tray_now=255,
        last_loaded_tray=0,
        tray_change_log=[(0, 0)],
        connected=True,
    )
    return SimpleNamespace(get_status=lambda _pid: status)


async def _book(db, printer, archive, *, analysis=ANALYSIS, attempted=True):
    """The tracker's own completion: 300 g of the 3MF on AMS0-T0."""
    return await usage_tracker.on_print_complete(
        printer.id,
        {"status": "completed"},
        _printer_manager(50),
        db,
        archive_id=archive.id,
        ams_mapping=[0],
        file_analysis=analysis,
        analysis_attempted=attempted,
    )


async def _books(db, spool) -> tuple[float, list[tuple[str, float]]]:
    await db.refresh(spool)
    rows = (
        (await db.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool.id).order_by("id")))
        .scalars()
        .all()
    )
    return round(spool.weight_used, 1), [(r.status, r.weight_used) for r in rows]


def _session(printer_id: int, spool_id: int, archive: PrintArchive) -> PrintSession:
    return PrintSession(
        printer_id=printer_id,
        print_name="bracket",
        started_at=datetime.now(timezone.utc),
        tray_remain_start={(0, 0): 80},
        tray_now_at_start=0,
        spool_assignments={(0, 0): spool_id},
        ams_mapping=[0],
        archive_identity=archive.recovery_identity,
        owner_tracked=True,
    )


# ── the predicate ───────────────────────────────────────────────────────────


class TestThePredicate:
    async def test_an_idle_printer_with_nothing_owed_may_be_synced(self):
        assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="IDLE")) is False
        assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="FINISH")) is False
        assert usage_tracker.print_is_unbooked(7, None) is False

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "running"])
    async def test_a_printing_printer_is_unbooked_whatever_the_session_says(self, state):
        assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state=state)) is True

    async def test_a_registered_session_is_unbooked_whatever_the_printer_says(self):
        usage_tracker._active_sessions[7] = object()
        assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="FINISH")) is True
        assert usage_tracker.print_is_unbooked(8, SimpleNamespace(state="FINISH")) is False

    async def test_a_completion_in_progress_is_unbooked(self):
        with usage_tracker.completion_in_progress(7):
            assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="FINISH")) is True
            assert usage_tracker.print_is_unbooked(8, SimpleNamespace(state="FINISH")) is False
        assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="FINISH")) is False


# ── a mark never outlives its completion ────────────────────────────────────


class TestTheMarkEndsWithItsCompletion:
    async def test_an_exception_inside_the_completion_clears_it(self):
        with pytest.raises(RuntimeError), usage_tracker.completion_in_progress(7):
            raise RuntimeError("the completion broke")
        assert usage_tracker._completing == {}

    async def test_two_overlapping_completions_each_end_their_own(self):
        with usage_tracker.completion_in_progress(7):
            with usage_tracker.completion_in_progress(7):
                pass
            assert usage_tracker.print_is_unbooked(7, None) is True
        assert usage_tracker._completing == {}

    async def test_the_real_completion_callback_marks_and_clears_when_it_raises(self):
        seen = []

        async def _impl(printer_id, data, analysis_completion):
            seen.append(usage_tracker.print_is_unbooked(printer_id, SimpleNamespace(state="FINISH")))
            raise RuntimeError("archive lookup failed")

        with (
            patch.object(main_module, "_on_print_complete_impl", _impl),
            patch.object(main_module, "_release_unseeded_journal", AsyncMock()),
            pytest.raises(RuntimeError),
        ):
            await main_module.on_print_complete(7, {"status": "completed"})

        assert seen == [True]
        assert usage_tracker._completing == {}

    async def test_a_cancelled_completion_task_clears_it(self):
        entered = asyncio.Event()

        async def _impl(printer_id, data, analysis_completion):
            entered.set()
            await asyncio.Event().wait()  # the swap macro that never returns

        with (
            patch.object(main_module, "_on_print_complete_impl", _impl),
            patch.object(main_module, "_release_unseeded_journal", AsyncMock()),
        ):
            task = asyncio.create_task(main_module.on_print_complete(7, {"status": "completed"}))
            await entered.wait()
            assert usage_tracker.print_is_unbooked(7, SimpleNamespace(state="FINISH")) is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert usage_tracker._completing == {}

    async def test_the_next_print_start_clears_a_mark_left_behind(self, db_session, printer_factory):
        """Belt and braces: whatever kept a mark, the next print on that printer is a new question."""
        printer = await printer_factory()
        usage_tracker._completing[printer.id] = {object()}

        await usage_tracker.on_print_start(
            printer.id, {"subtask_name": "next"}, _printer_manager(80, "RUNNING"), db=db_session
        )

        assert printer.id not in usage_tracker._completing

    async def test_a_restart_carries_no_mark(self, db_session, printer_factory):
        """The mark is process memory only. A process that died inside a completion comes back with the
        persisted session and nothing else: restoring it marks nothing."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)
        with usage_tracker.completion_in_progress(printer.id):
            await usage_tracker.persist_session(db_session, _session(printer.id, spool.id, archive))
            usage_tracker._completing.clear()  # the process died here
            usage_tracker._active_sessions.clear()

        await usage_tracker.restore_session(db_session, printer.id)

        assert usage_tracker._completing == {}
        assert printer.id in usage_tracker._active_sessions

    async def test_the_trackers_own_completion_clears_it_on_the_rollback_path(
        self, db_session, printer_factory, main_db
    ):
        """The archive's id was reused under the booking: nothing is charged, and nothing stays marked."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)

        results = await usage_tracker.on_print_complete(
            printer.id,
            {"status": "completed"},
            _printer_manager(50),
            db_session,
            archive_id=archive.id,
            ams_mapping=[0],
            file_analysis=ANALYSIS,
            analysis_attempted=True,
            expected_epoch=-1,  # the storage moved on: the tracker rolls back
        )

        assert results == []
        assert usage_tracker._completing == {}
        assert (await _books(db_session, spool))[0] == 200.0


# ── the automatic sync, built-in inventory ──────────────────────────────────


class TestTheAutomaticSync:
    async def test_idle_the_books_still_catch_up_with_the_ams(self, db_session, printer_factory, main_db):
        """Unchanged: filament this instance never saw leave the reel is reconciled while nothing prints."""
        printer, spool = await _reel(db_session, printer_factory)

        await _push(printer.id, 65, "IDLE")

        assert await _books(db_session, spool) == (350.0, [("ams_sync", 150.0)])

    async def test_a_restart_that_restored_no_session_does_not_sync_the_running_print(
        self, db_session, printer_factory, main_db
    ):
        """650 before: the push at 50 % wrote 350, the completion added the 3MF's 300."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)
        assert printer.id not in usage_tracker._active_sessions  # the restart restored nothing

        await _push(printer.id, 65, "RUNNING")
        await _book(db_session, printer, archive)

        assert await _books(db_session, spool) == (500.0, [("completed", 300.0)])

    async def test_a_push_between_the_session_leaving_memory_and_the_booking_is_not_synced(
        self, db_session, printer_factory, main_db
    ):
        """800 before: the printer says FINISH, the session is popped, the file analysis is awaited --
        the push wrote 500 and the tracker added 300 on top."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, archive)

        async def _analysis_while_the_ams_reports(*args, **kwargs):
            assert printer.id not in usage_tracker._active_sessions
            await _push(printer.id, 50, "FINISH")
            return ANALYSIS

        with patch("backend.app.services.print_file_analysis.get_print_file_analysis", _analysis_while_the_ams_reports):
            await _book(db_session, printer, archive, analysis=None, attempted=False)

        assert await _books(db_session, spool) == (500.0, [("completed", 300.0)])

    async def test_once_the_print_is_booked_the_sync_resumes(self, db_session, printer_factory, main_db):
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, archive)
        await _book(db_session, printer, archive)

        await _push(printer.id, 45, "FINISH")  # 50 g the print did not account for: a purge

        assert await _books(db_session, spool) == (550.0, [("completed", 300.0), ("ams_sync", 50.0)])

    async def test_the_tracker_charges_a_normal_print_exactly_as_before(self, db_session, printer_factory, main_db):
        """GUARD 1: session registered, no push at the wrong moment -- the same rows, grams and cost."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)
        usage_tracker._active_sessions[printer.id] = _session(printer.id, spool.id, archive)
        await _push(printer.id, 65, "RUNNING")  # skipped today as well: a session is registered

        results = await _book(db_session, printer, archive)

        assert results == [
            {
                "spool_id": spool.id,
                "weight_used": 300.0,
                "percent_used": 30,
                "ams_id": 0,
                "tray_id": 0,
                "material": "PLA",
                "cost": 6.0,
                "slot_id": 1,
                "color": "#FF0000",
            }
        ]
        assert await _books(db_session, spool) == (500.0, [("completed", 300.0)])
        await db_session.refresh(archive)
        assert archive.cost == 6.0


# ── the Sync button, built-in inventory ─────────────────────────────────────


def _button_state(remain: int, state: str, *, connected: bool = True):
    return SimpleNamespace(state=state, raw_data={"ams": _ams(remain)}, connected=connected)


class TestTheSyncButton:
    URL = "/api/v1/inventory/sync-ams-weights"

    async def _press(self, async_client, state):
        with patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=state):
            return await async_client.post(self.URL)

    async def test_idle_it_writes_the_ams_reading(self, async_client, db_session, printer_factory):
        _printer, spool = await _reel(db_session, printer_factory)

        response = await self._press(async_client, _button_state(65, "IDLE"))

        assert response.json() == {"synced": 1, "skipped": 0}
        assert await _books(db_session, spool) == (350.0, [("ams_sync", 150.0)])

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE"])
    async def test_pressed_mid_print_it_leaves_the_printing_printer_alone_and_says_so(
        self, async_client, db_session, printer_factory, state
    ):
        """650 before: 350 written by the button at 50 %, 300 added by the completion."""
        printer, spool = await _reel(db_session, printer_factory)
        archive = await _archive(db_session, printer.id)

        response = await self._press(async_client, _button_state(65, state))

        assert response.json() == {"synced": 0, "skipped": 1, "skipped_printers": 1}
        await _book(db_session, printer, archive)
        assert await _books(db_session, spool) == (500.0, [("completed", 300.0)])

    async def test_pressed_while_the_print_is_being_closed_out_it_waits_too(
        self, async_client, db_session, printer_factory
    ):
        printer, spool = await _reel(db_session, printer_factory)

        with usage_tracker.completion_in_progress(printer.id):
            response = await self._press(async_client, _button_state(50, "FINISH"))

        assert response.json() == {"synced": 0, "skipped": 1, "skipped_printers": 1}
        assert await _books(db_session, spool) == (200.0, [])

    async def test_the_spools_are_written_in_ascending_id_order(self, async_client, db_session, printer_factory):
        """Each write holds its spool's row until the request commits. Two presses that walked the
        assignments in different orders could hold each other's rows on PostgreSQL: one order, by id."""
        printer = await printer_factory()
        spools = []
        for _ in range(3):
            spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=200)
            db_session.add(spool)
            await db_session.commit()
            await db_session.refresh(spool)
            spools.append(spool)
        for tray_id, spool in zip(range(3), reversed(spools), strict=True):  # rows in descending spool order
            db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=tray_id))
            await db_session.commit()
        trays = [{"id": t, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 65} for t in range(3)]
        state = SimpleNamespace(state="IDLE", raw_data={"ams": [{"id": 0, "tray": trays}]}, connected=True)
        written: list[int] = []
        real = usage_tracker.sync_spool_weight_from_ams

        async def recording(db, spool, **kwargs):
            written.append(spool.id)
            return await real(db, spool, **kwargs)

        with patch.object(usage_tracker, "sync_spool_weight_from_ams", recording):
            response = await self._press(async_client, state)

        assert response.json() == {"synced": 3, "skipped": 0}
        assert written == sorted(s.id for s in spools)

    async def test_a_printer_that_dropped_off_is_not_read_from_its_last_report(
        self, async_client, db_session, printer_factory
    ):
        _printer, spool = await _reel(db_session, printer_factory)

        response = await self._press(async_client, _button_state(65, "IDLE", connected=False))

        assert response.json() == {"synced": 0, "skipped": 1}
        assert await _books(db_session, spool) == (200.0, [])


# ── Spoolman ────────────────────────────────────────────────────────────────


@pytest.fixture
async def spoolman_on(db_session):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://spoolman.test"))
    await db_session.commit()


def _tagged_spoolman_spool() -> dict:
    # 1 kg, 800 g left by Spoolman's books -- the same reel as above.
    return {"id": 42, "initial_weight": 1000, "remaining_weight": 800.0, "filament": {"weight": 1000}}


def _spoolman_client() -> SpoolmanClient:
    client = SpoolmanClient("http://spoolman.test")
    client.health_check = AsyncMock(return_value=True)
    client.get_spools = AsyncMock(return_value=[_tagged_spoolman_spool()])
    client.get_all_spools = AsyncMock(return_value=[_tagged_spoolman_spool()])
    client.sync_ams_tray = AsyncMock(return_value=_tagged_spoolman_spool())
    client.clear_location_for_removed_spools = AsyncMock(return_value=0)
    client.use_spool = AsyncMock(return_value={})
    client.update_spool = AsyncMock(return_value={})
    return client


class TestSpoolmanTaggedSpools:
    async def _push(self, printer_id, remain, state):
        client = _spoolman_client()
        with patch("backend.app.main.get_spoolman_client", AsyncMock(return_value=client)):
            await _push(printer_id, remain, state)
        return client

    async def test_idle_the_ams_reading_is_used_as_before(self, db_session, printer_factory, main_db, spoolman_on):
        printer = await printer_factory()

        client = await self._push(printer.id, 50, "FINISH")

        client.use_spool.assert_awaited_once_with(42, 300.0)

    async def test_between_finish_and_the_usage_report_the_reading_is_not_used(
        self, db_session, printer_factory, main_db, spoolman_on
    ):
        """300 g twice before: ``use_spool(300)`` here during the swap macro, then the 3MF's 300 g."""
        printer = await printer_factory()

        with usage_tracker.completion_in_progress(printer.id):
            client = await self._push(printer.id, 50, "FINISH")

        client.sync_ams_tray.assert_awaited_once()  # the metadata sync still runs
        client.use_spool.assert_not_awaited()

    async def test_while_printing_it_is_not_used_either(self, db_session, printer_factory, main_db, spoolman_on):
        printer = await printer_factory()

        client = await self._push(printer.id, 65, "RUNNING")

        client.use_spool.assert_not_awaited()


class TestTheSpoolmanSyncButton:
    URL = "/api/v1/spoolman/inventory/sync-ams-weights"

    @pytest.fixture
    def spoolman(self, spoolman_on):
        from backend.app.api.routes import spoolman_inventory
        from backend.tests.unit.services.spoolman_weighing_fake import WeighingSpoolman, client_for

        fake = WeighingSpoolman()
        fake.add_vendor(3, "Bambu Lab")
        fake.add_filament(7, vendor_id=3, name="PLA Basic", material="PLA", color_hex="FF0000", weight=1000)
        client = client_for(fake)
        spoolman_inventory._health_check_cache.clear()
        with (
            patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
        ):
            yield fake
        spoolman_inventory._health_check_cache.clear()

    async def _assigned(self, db, printer_factory, spoolman):
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

        printer = await printer_factory()
        spoolman.add_spool(42, 7, initial_weight=1000, used_weight=200.0)
        db.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=42))
        await db.commit()
        return printer

    async def _press(self, async_client, state):
        with patch("backend.app.api.routes.spoolman_inventory.printer_manager.get_status", return_value=state):
            return await async_client.post(self.URL)

    async def test_idle_it_writes_what_is_left(self, async_client, db_session, printer_factory, spoolman):
        await self._assigned(db_session, printer_factory, spoolman)

        response = await self._press(async_client, _button_state(65, "IDLE"))

        assert response.json() == {"synced": 1, "skipped": 0}
        assert spoolman.view(42)["remaining_weight"] == 650.0

    async def test_pressed_mid_print_it_leaves_the_printing_printer_alone_and_says_so(
        self, async_client, db_session, printer_factory, spoolman
    ):
        """350 g left before: the button wrote 650 at 50 %, the report then used the 3MF's 300."""
        await self._assigned(db_session, printer_factory, spoolman)

        response = await self._press(async_client, _button_state(65, "RUNNING"))

        assert response.json() == {"synced": 0, "skipped": 1, "skipped_printers": 1}
        assert spoolman.view(42)["remaining_weight"] == 800.0

    async def test_pressed_while_the_print_is_being_closed_out_it_waits_too(
        self, async_client, db_session, printer_factory, spoolman
    ):
        printer = await self._assigned(db_session, printer_factory, spoolman)

        with usage_tracker.completion_in_progress(printer.id):
            response = await self._press(async_client, _button_state(50, "FINISH"))

        assert response.json() == {"synced": 0, "skipped": 1, "skipped_printers": 1}
        assert spoolman.view(42)["remaining_weight"] == 800.0
