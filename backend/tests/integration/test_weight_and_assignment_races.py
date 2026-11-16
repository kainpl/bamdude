"""Weight and assignment races (upstream 5b34997df, 0b1d6c238).

- A weight typed by hand is the operator's measurement. The remain% sync that had already compared the old
  figure wrote the AMS reading over it and left the hand-weight lock standing over the wrong number: books
  200, the operator weighs the reel and saves 480, the sync that read 200 writes 500 -- locked at 500.
- "Sync weights from AMS" leaves a hand-weighed spool alone; the lock is released only by an explicit unlock,
  on one spool or on a selection.
- A print's booking holds the spool rows it charges until it commits (PostgreSQL; SQLite already holds its
  one writer), so a hand weight saved meanwhile waits and stands. Arrival order decides, on both databases.
- Unassign and "add from slot" wait for the printer's assignment lock, as assign and the AMS callback do,
  and "add from slot" looks the tag up again under it: a double click made two spools with one tag.

PostgreSQL: the row hold itself cannot be exercised here (the suite runs on SQLite, where ``FOR UPDATE`` is
not emitted). What is pinned is the statement PostgreSQL receives, and that SQLite is charged exactly as before.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app import main as main_module
from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import usage_tracker
from backend.app.services.print_file_analysis import PrintFileAnalysis

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
TAG = "0102030405060708"
API = "/api/v1/inventory"


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    saved = dict(main_module._ams_assignment_locks)
    main_module._ams_assignment_locks.clear()
    yield
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    main_module._ams_assignment_locks.clear()
    main_module._ams_assignment_locks.update(saved)


@pytest.fixture
def other_session(test_engine):
    """A second connection's worth of session: the operator's request, apart from the one under test."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _reel(db, printer_factory, *, weight_used=200.0, locked=False, assigned=True):
    printer = await printer_factory()
    spool = Spool(
        material="PLA",
        rgba="FF0000FF",
        label_weight=1000,
        weight_used=weight_used,
        weight_locked=locked,
        tray_uuid=UUID,
        cost_per_kg=20.0,
    )
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    if assigned:
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


async def _stored(db, spool_id) -> tuple[float, bool, list[tuple[str, float]]]:
    row = (
        await db.execute(select(Spool).where(Spool.id == spool_id).execution_options(populate_existing=True))
    ).scalar_one()
    history = (
        (await db.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool_id).order_by("id")))
        .scalars()
        .all()
    )
    return round(row.weight_used, 1), bool(row.weight_locked), [(h.status, h.weight_used) for h in history]


async def _weigh_by_hand(other_session, spool_id: int, weight_used: float) -> None:
    """The operator's save, as ``inventory_service.update_spool`` writes it: the weight and its lock."""
    async with other_session() as db:
        row = await db.get(Spool, spool_id)
        row.weight_used = weight_used
        row.weight_locked = True
        await db.commit()


def _tray(remain: int) -> dict:
    return {
        "id": 0,
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tray_uuid": UUID,
        "tag_uid": TAG,
        "remain": remain,
    }


def _state(remain: int):
    return SimpleNamespace(state="IDLE", connected=True, raw_data={"ams": [{"id": 0, "tray": [_tray(remain)]}]})


# ── the remain% sync and a hand weight ──────────────────────────────────────


class TestTheSyncNeverOverwritesAHandWeight:
    async def test_a_weight_saved_after_the_sync_compared_stands_with_its_lock(
        self, db_session, printer_factory, other_session
    ):
        """Was (500.0, True, [("ams_sync", 300.0)]): the AMS figure, under the hand-weight lock."""
        printer, spool = await _reel(db_session, printer_factory)
        assert spool.weight_used == 200.0  # what the sync loaded and compared with the AMS's 500

        await _weigh_by_hand(other_session, spool.id, 480.0)
        written = await usage_tracker.sync_spool_weight_from_ams(
            db_session, spool, printer_id=printer.id, ams_id=0, tray_id=0, new_used=500.0
        )
        await db_session.commit()

        assert written == 0.0
        assert await _stored(db_session, spool.id) == (480.0, True, [])

    async def test_a_booking_that_landed_meanwhile_is_not_undone(self, db_session, printer_factory, other_session):
        """The sync compared 200; a print booked 400 since. The AMS's 500 is decided again on the next push."""
        printer, spool = await _reel(db_session, printer_factory)
        async with other_session() as db:
            (await db.get(Spool, spool.id)).weight_used = 600.0
            await db.commit()

        written = await usage_tracker.sync_spool_weight_from_ams(
            db_session, spool, printer_id=printer.id, ams_id=0, tray_id=0, new_used=500.0
        )
        await db_session.commit()

        assert written == 0.0
        assert await _stored(db_session, spool.id) == (600.0, False, [])

    async def test_an_undisturbed_spool_is_synced_exactly_as_before(self, db_session, printer_factory):
        printer, spool = await _reel(db_session, printer_factory)

        written = await usage_tracker.sync_spool_weight_from_ams(
            db_session, spool, printer_id=printer.id, ams_id=0, tray_id=0, new_used=350.0
        )
        await db_session.commit()

        assert written == 150.0
        assert await _stored(db_session, spool.id) == (350.0, False, [("ams_sync", 150.0)])

    async def test_the_automatic_sync_goes_through_the_same_door(self, db_session, printer_factory, other_session):
        """The real AMS callback: the hand weight lands while the callback is between its read and its write."""
        printer, spool = await _reel(db_session, printer_factory)

        @asynccontextmanager
        async def _session_ctx():
            yield db_session

        real = usage_tracker.sync_spool_weight_from_ams

        async def _operator_saves_first(db, loaded, **kwargs):
            await _weigh_by_hand(other_session, loaded.id, 480.0)
            return await real(db, loaded, **kwargs)

        status = SimpleNamespace(state="IDLE", raw_data={}, connected=True)
        with (
            patch("backend.app.main.async_session", _session_ctx),
            patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
            patch.object(main_module.printer_manager, "get_status", return_value=status),
            patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
            patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
            patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
            patch("backend.app.services.usage_tracker.sync_spool_weight_from_ams", _operator_saves_first),
        ):
            await main_module.on_ams_change(printer.id, [{"id": 0, "tray": [_tray(50)]}])

        assert await _stored(db_session, spool.id) == (480.0, True, [])


# ── the Sync button and the lock ────────────────────────────────────────────


class TestTheSyncButtonHonoursTheLock:
    async def _press(self, async_client, remain=50):
        with patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=_state(remain)):
            return await async_client.post(f"{API}/sync-ams-weights")

    async def test_a_hand_weighed_spool_keeps_its_weight_and_its_lock(self, async_client, db_session, printer_factory):
        """Upstream's sync writes it and drops the lock; ours leaves the measurement alone (owner, P17)."""
        _printer, spool = await _reel(db_session, printer_factory, weight_used=480.0, locked=True)

        response = await self._press(async_client)

        assert response.json() == {"synced": 0, "skipped": 1}
        assert await _stored(db_session, spool.id) == (480.0, True, [])

    async def test_once_unlocked_on_the_spool_the_button_syncs_it(self, async_client, db_session, printer_factory):
        _printer, spool = await _reel(db_session, printer_factory, weight_used=480.0, locked=True)

        unlocked = await async_client.patch(f"{API}/spools/{spool.id}", json={"weight_locked": False})
        assert unlocked.status_code == 200
        assert unlocked.json()["weight_locked"] is False
        assert unlocked.json()["weight_used"] == 480.0  # unlocking is not a weight entry
        response = await self._press(async_client)

        assert response.json() == {"synced": 1, "skipped": 0}
        assert await _stored(db_session, spool.id) == (500.0, False, [("ams_sync", 20.0)])

    async def test_entering_a_weight_locks_again(self, async_client, db_session, printer_factory):
        _printer, spool = await _reel(db_session, printer_factory, weight_used=480.0, locked=False)

        response = await async_client.patch(f"{API}/spools/{spool.id}", json={"weight_used": 470.0})

        assert response.json()["weight_locked"] is True


class TestBulkUnlock:
    async def test_the_selected_spools_are_unlocked_and_nothing_else_moves(
        self, async_client, db_session, printer_factory
    ):
        _p1, first = await _reel(db_session, printer_factory, weight_used=480.0, locked=True, assigned=False)
        _p2, second = await _reel(db_session, printer_factory, weight_used=120.0, locked=True, assigned=False)
        _p3, other = await _reel(db_session, printer_factory, weight_used=300.0, locked=True, assigned=False)

        response = await async_client.patch(
            f"{API}/spools/bulk-update",
            json={"spool_ids": [first.id, second.id], "fields": {"weight_locked": False}},
        )

        assert response.status_code == 200, response.text
        assert await _stored(db_session, first.id) == (480.0, False, [])
        assert await _stored(db_session, second.id) == (120.0, False, [])
        assert await _stored(db_session, other.id) == (300.0, True, [])  # not selected

    async def test_a_bulk_edit_can_never_lock(self, async_client, db_session, printer_factory):
        """A lock is set by weighing one spool; a selection is never handed one."""
        _p, spool = await _reel(db_session, printer_factory, weight_used=480.0, locked=False, assigned=False)

        response = await async_client.patch(
            f"{API}/spools/bulk-update",
            json={"spool_ids": [spool.id], "fields": {"weight_locked": True, "note": "shelf B"}},
        )

        assert response.status_code == 200, response.text
        assert await _stored(db_session, spool.id) == (480.0, False, [])

    async def test_a_bulk_edit_still_never_writes_a_weight(self, async_client, db_session, printer_factory):
        _p, spool = await _reel(db_session, printer_factory, weight_used=480.0, locked=True, assigned=False)

        response = await async_client.patch(
            f"{API}/spools/bulk-update",
            json={"spool_ids": [spool.id], "fields": {"weight_locked": False, "weight_used": 0}},
        )

        assert response.status_code == 200, response.text
        assert await _stored(db_session, spool.id) == (480.0, False, [])


# ── a print's booking and a hand weight ─────────────────────────────────────

ANALYSIS = PrintFileAnalysis(
    filament_usage=[{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}],
    layer_usage=None,
    filament_properties={},
)


async def _book(db, printer, spool):
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
        state="FINISH", raw_data={"ams": [{"id": 0, "tray": [_tray(50)]}]}, tray_now=255, last_loaded_tray=0
    )
    status.tray_change_log = [(0, 0)]
    return await usage_tracker.on_print_complete(
        printer.id,
        {"status": "completed"},
        SimpleNamespace(get_status=lambda _pid: status),
        db,
        archive_id=archive.id,
        ams_mapping=[0],
        file_analysis=ANALYSIS,
        analysis_attempted=True,
    )


class TestABookingAndAHandWeight:
    def test_postgresql_holds_the_spool_row_for_the_booking(self):
        statement = str(usage_tracker._spool_charge_stmt(57).compile(dialect=postgresql.dialect()))

        assert statement.rstrip().endswith("FOR UPDATE")
        assert "FROM spool" in statement

    def test_sqlite_is_sent_the_same_read_as_before(self):
        """Its one writer is already the booking's (``_lock_usage_archive``): nothing to add."""
        statement = str(usage_tracker._spool_charge_stmt(57).compile(dialect=sqlite.dialect()))

        assert "FOR UPDATE" not in statement

    async def test_a_weight_entered_after_the_booking_stands(self, db_session, printer_factory, other_session):
        printer, spool = await _reel(db_session, printer_factory)

        await _book(db_session, printer, spool)
        await _weigh_by_hand(other_session, spool.id, 480.0)

        assert await _stored(db_session, spool.id) == (480.0, True, [("completed", 300.0)])

    async def test_a_weight_entered_before_the_booking_gets_the_print_on_top(
        self, db_session, printer_factory, other_session
    ):
        """Arrival order, on both databases (owner, P39) -- and read fresh, never from a copy loaded earlier."""
        printer, spool = await _reel(db_session, printer_factory)
        assert spool.weight_used == 200.0  # the booking's session already holds this copy

        await _weigh_by_hand(other_session, spool.id, 480.0)
        await _book(db_session, printer, spool)

        assert await _stored(db_session, spool.id) == (780.0, True, [("completed", 300.0)])

    async def test_a_normal_print_is_charged_exactly_as_before(self, db_session, printer_factory):
        """GUARD 1."""
        printer, spool = await _reel(db_session, printer_factory)

        results = await _book(db_session, printer, spool)

        assert [(r["spool_id"], r["weight_used"], r["cost"], r["slot_id"]) for r in results] == [
            (spool.id, 300.0, 6.0, 1)
        ]
        assert await _stored(db_session, spool.id) == (500.0, False, [("completed", 300.0)])


# ── the slot lock ───────────────────────────────────────────────────────────


async def _while_the_lock_is_held(printer_id: int, request) -> tuple[bool, object]:
    """Start ``request`` while the AMS callback holds the printer's assignment lock.

    Returns whether the request had finished before the lock was released, and its response."""
    lock = main_module._get_ams_assignment_lock(printer_id)
    await lock.acquire()
    try:
        task = asyncio.create_task(request())
        done, _pending = await asyncio.wait({task}, timeout=0.5)
        finished_under_the_lock = bool(done)
    finally:
        lock.release()
    return finished_under_the_lock, await task


class TestUnassignWaitsForTheSlotLock:
    async def test_it_waits_and_then_removes_the_row_once(self, async_client, db_session, printer_factory):
        printer, spool = await _reel(db_session, printer_factory)

        early, response = await _while_the_lock_is_held(
            printer.id, lambda: async_client.delete(f"{API}/assignments/{printer.id}/0/0")
        )

        assert early is False
        assert response.status_code == 200
        rows = (await db_session.execute(select(func.count()).select_from(SpoolAssignment))).scalar_one()
        assert rows == 0

    async def test_a_row_the_callback_removed_meanwhile_is_a_404_not_a_500(
        self, async_client, db_session, printer_factory, other_session
    ):
        printer, spool = await _reel(db_session, printer_factory)
        lock = main_module._get_ams_assignment_lock(printer.id)
        await lock.acquire()
        task = asyncio.create_task(async_client.delete(f"{API}/assignments/{printer.id}/0/0"))
        await asyncio.sleep(0.2)
        async with other_session() as db:  # the callback's unlink pass, under its lock
            row = (await db.execute(select(SpoolAssignment))).scalar_one()
            await db.delete(row)
            await db.commit()
        lock.release()

        response = await task

        assert response.status_code == 404


def _tagged_slot():
    return SimpleNamespace(
        state="IDLE",
        connected=True,
        layer_num=0,
        tray_now=255,
        raw_data={"ams": {"ams": [{"id": 0, "tray": [_tray(80)]}]}},
    )


class TestAddFromSlot:
    async def _add(self, async_client, printer_id):
        # ``ensure_fresh_connection`` opens a session of its own, and in this suite every session shares one
        # connection: closing it would roll the request's uncommitted spool back.
        with (
            patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=_tagged_slot()),
            patch("backend.app.services.printer_manager.printer_manager.ensure_fresh_connection", AsyncMock()),
        ):
            return await async_client.post(
                f"{API}/spools/from-slot", json={"printer_id": printer_id, "ams_id": 0, "tray_id": 0}
            )

    async def test_it_waits_for_the_slot_lock(self, async_client, db_session, printer_factory):
        printer = await printer_factory()

        early, response = await _while_the_lock_is_held(printer.id, lambda: self._add(async_client, printer.id))

        assert early is False
        assert response.status_code == 200

    async def test_a_second_click_makes_no_second_spool(self, async_client, db_session, printer_factory):
        """Was two spools with one tag, the first of them out of its slot."""
        printer = await printer_factory()

        first = await self._add(async_client, printer.id)
        second = await self._add(async_client, printer.id)

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["id"] == first.json()["id"]
        spools = (await db_session.execute(select(Spool).where(func.upper(Spool.tray_uuid) == UUID))).scalars().all()
        assert len(spools) == 1
        rows = (await db_session.execute(select(SpoolAssignment))).scalars().all()
        assert [(r.printer_id, r.ams_id, r.tray_id, r.spool_id) for r in rows] == [(printer.id, 0, 0, spools[0].id)]

    async def test_a_reel_the_rfid_pass_registered_meanwhile_is_reused(
        self, async_client, db_session, printer_factory, other_session
    ):
        printer = await printer_factory()
        lock = main_module._get_ams_assignment_lock(printer.id)
        await lock.acquire()
        task = asyncio.create_task(self._add(async_client, printer.id))
        await asyncio.sleep(0.2)
        async with other_session() as db:  # the AMS callback's own auto-create, under its lock
            db.add(Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=0, tray_uuid=UUID))
            await db.commit()
        lock.release()

        response = await task

        assert response.status_code == 200
        spools = (await db_session.execute(select(Spool).where(func.upper(Spool.tray_uuid) == UUID))).scalars().all()
        assert [s.id for s in spools] == [response.json()["id"]]
