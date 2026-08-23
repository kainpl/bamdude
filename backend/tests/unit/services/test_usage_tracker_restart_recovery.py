"""The filament-attribution context has to outlive the process.

Ported from upstream `454457a0`. Everything the completion path needs to split a
print's filament across the trays that fed it lived only in memory: the
dispatched slot→tray mapping, the spool-assignment snapshot, and the tray-change
log. A print that outlived a restart lost all of it and fell back to what the
printer reports at completion — which, with AMS filament backup on, is the
substitute tray. The whole print was charged to the spool that only finished it;
the spool that ran dry was charged nothing.

⚠️ The failure is silent and one-directional: no error, no missing row, just
grams on the wrong spool — and it needs a restart *during* a long print to
happen, which is exactly when nobody is watching the numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.active_print_session import ActivePrintSession
from backend.app.services.usage_tracker import (
    PrintSession,
    _active_sessions,
    clear_persisted_session,
    discard_session,
    get_persisted_print_name,
    on_print_start,
    persist_session,
    record_tray_change,
    restore_session,
)


@pytest.fixture(autouse=True)
def _clean_memory():
    _active_sessions.clear()
    yield
    _active_sessions.clear()


def _session(printer_id: int, **kw) -> PrintSession:
    args = {
        "printer_id": printer_id,
        "print_name": "gridfinity_bin.gcode.3mf",
        "started_at": datetime.now(timezone.utc),
        "tray_remain_start": {(0, 0): 80, (0, 1): 45},
        "tray_now_at_start": 0,
        "spool_assignments": {(0, 0): 12, (0, 1): 34},
        "ams_mapping": [0],
    }
    args.update(kw)
    return PrintSession(**args)


class TestTheRowRoundTrips:
    @pytest.mark.asyncio
    async def test_everything_the_completion_path_reads_comes_back(self, db_session, printer_factory):
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id), [(0, 0), (1, 120)])

        _active_sessions.clear()
        log = await restore_session(db_session, printer.id)
        restored = _active_sessions[printer.id]

        assert restored.print_name == "gridfinity_bin.gcode.3mf"
        assert restored.ams_mapping == [0]
        assert restored.tray_now_at_start == 0
        assert log == [[0, 0], [1, 120]]

    @pytest.mark.asyncio
    async def test_tray_keys_survive_the_json_round_trip(self, db_session, printer_factory):
        """⚠️ Both maps are keyed by ``(ams_id, tray_id)`` tuples, and JSON has no
        tuple. Coming back as the string ``"0-1"`` would match no tray at all and
        the print would silently lose its assignment snapshot."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id))

        await restore_session(db_session, printer.id)
        restored = _active_sessions[printer.id]

        assert restored.spool_assignments == {(0, 0): 12, (0, 1): 34}
        assert restored.tray_remain_start == {(0, 0): 80, (0, 1): 45}

    @pytest.mark.asyncio
    async def test_a_naive_started_at_comes_back_as_utc(self, db_session, printer_factory):
        """SQLite hands back a naive datetime; comparing it to an aware one raises."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id))

        await restore_session(db_session, printer.id)

        assert _active_sessions[printer.id].started_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_nothing_persisted_restores_nothing(self, db_session, printer_factory):
        printer = await printer_factory()

        assert await restore_session(db_session, printer.id) is None
        assert printer.id not in _active_sessions

    @pytest.mark.asyncio
    async def test_a_second_print_start_overwrites_the_row(self, db_session, printer_factory):
        """A printer runs one print at a time. A row left by a completion we
        never saw must not outlive the next print start."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id, print_name="old.3mf"), [(0, 0)])
        await persist_session(db_session, _session(printer.id, print_name="new.3mf"), None)

        rows = (await db_session.execute(select(ActivePrintSession))).scalars().all()

        assert [r.print_name for r in rows] == ["new.3mf"]
        assert rows[0].tray_change_log is None


class TestRegisteringTheSession:
    @pytest.mark.asyncio
    async def test_spoolman_gets_the_log_without_the_in_memory_session(self, db_session, printer_factory):
        """``_active_sessions`` doubles as ``on_ams_change``'s "skip the remain%
        weight sync" flag. Registering a session the internal tracker will never
        complete would suppress a sync Spoolman users still need."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id), [(0, 0)])

        log = await restore_session(db_session, printer.id, register_active=False)

        assert log == [[0, 0]]
        assert printer.id not in _active_sessions


class TestTheTrayChangeLog:
    @pytest.mark.asyncio
    async def test_a_change_is_appended(self, db_session, printer_factory):
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id), [(0, 0)])

        await record_tray_change(db_session, printer.id, 1, 250)

        assert await restore_session(db_session, printer.id) == [[0, 0], [1, 250]]

    @pytest.mark.asyncio
    async def test_the_seeded_entry_is_not_duplicated(self, db_session, printer_factory):
        """Print start seeds the log from PrinterState, which may already hold
        the change this callback is reporting."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id), [(0, 0)])

        await record_tray_change(db_session, printer.id, 0, 0)

        assert await restore_session(db_session, printer.id) == [[0, 0]]

    @pytest.mark.asyncio
    async def test_the_same_tray_at_a_later_layer_is_a_real_change(self, db_session, printer_factory):
        """A backup swap and a swap back is two boundaries, not one."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id), [(0, 0)])

        await record_tray_change(db_session, printer.id, 1, 100)
        await record_tray_change(db_session, printer.id, 0, 180)

        assert await restore_session(db_session, printer.id) == [[0, 0], [1, 100], [0, 180]]

    @pytest.mark.asyncio
    async def test_a_change_outside_a_tracked_print_is_a_no_op(self, db_session, printer_factory):
        printer = await printer_factory()

        await record_tray_change(db_session, printer.id, 1, 10)

        assert (await db_session.execute(select(ActivePrintSession))).scalars().all() == []


class TestClosingOut:
    @pytest.mark.asyncio
    async def test_discard_clears_memory_and_disk(self, db_session, printer_factory):
        printer = await printer_factory()
        _active_sessions[printer.id] = _session(printer.id)
        await persist_session(db_session, _active_sessions[printer.id], [(0, 0)])

        await discard_session(db_session, printer.id)

        assert printer.id not in _active_sessions
        assert (await db_session.execute(select(ActivePrintSession))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_clearing_a_row_that_is_not_there_does_not_raise(self, db_session, printer_factory):
        printer = await printer_factory()

        await clear_persisted_session(db_session, printer.id)

    @pytest.mark.asyncio
    async def test_the_persisted_name_is_readable_for_the_identity_check(self, db_session, printer_factory):
        """Restart recovery refuses a row whose print isn't the one running."""
        printer = await printer_factory()
        await persist_session(db_session, _session(printer.id, print_name="left_behind.3mf"))

        assert await get_persisted_print_name(db_session, printer.id) == "left_behind.3mf"
        assert await get_persisted_print_name(db_session, printer.id + 999) is None


def _printer_manager_with_ams():
    state = MagicMock()
    state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]}
    state.tray_now = 0
    state.tray_change_log = [(0, 0)]
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


class TestCaptureAtPrintStart:
    @pytest.mark.asyncio
    async def test_the_row_is_written_and_the_session_registered(self, db_session, printer_factory):
        printer = await printer_factory()

        await on_print_start(
            printer.id,
            {"subtask_name": "bin.3mf", "ams_mapping": [2]},
            _printer_manager_with_ams(),
            db=db_session,
        )

        assert _active_sessions[printer.id].ams_mapping == [2]
        row = await db_session.get(ActivePrintSession, printer.id)
        # The tray log lives in the journal table since m153 — the row keeps
        # only session context, and its legacy column stays empty for new prints.
        assert (row.ams_mapping, row.tray_change_log) == ([2], None)

    @pytest.mark.asyncio
    async def test_spoolman_is_captured_too_but_not_registered(self, db_session, printer_factory):
        """The capture runs for both inventory backends — Spoolman's own durable
        row (#1820) does not carry the tray-change log, and that log is the only
        record of which spool fed which layers."""
        printer = await printer_factory()

        await on_print_start(
            printer.id,
            {"subtask_name": "bin.3mf", "ams_mapping": [2]},
            _printer_manager_with_ams(),
            db=db_session,
            spoolman_owns_usage=True,
        )

        assert printer.id not in _active_sessions
        assert (await db_session.get(ActivePrintSession, printer.id)) is not None

    @pytest.mark.asyncio
    async def test_a_failure_to_persist_does_not_fail_the_print_start(self, db_session, printer_factory):
        """Attribution is bookkeeping; the print is the job."""
        printer = await printer_factory()
        broken = AsyncMock()
        broken.get = AsyncMock(side_effect=RuntimeError("no database today"))
        broken.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )

        await on_print_start(printer.id, {"subtask_name": "bin.3mf"}, _printer_manager_with_ams(), db=broken)

        assert printer.id in _active_sessions


class TestCompletionAfterARestart:
    @pytest.mark.asyncio
    async def test_the_lost_session_is_picked_back_up_off_disk(self, db_session, printer_factory):
        """The whole point of the row: a print that outlived the process still
        gets its filament charged to the spool that was in the slot at start."""
        from backend.app.models.spool import Spool
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.services.usage_tracker import on_print_complete

        printer = await printer_factory()
        spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=0)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        await persist_session(
            db_session,
            _session(printer.id, tray_remain_start={(0, 0): 80}, spool_assignments={}),
        )
        _active_sessions.clear()  # the restart

        state = MagicMock()
        state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]}
        state.progress = 100
        state.layer_num = 0
        state.tray_now = 0
        state.tray_change_log = []
        pm = MagicMock()
        pm.get_status.return_value = state

        results = await on_print_complete(printer.id, {"status": "completed"}, pm, db_session)

        # 80% -> 70% of a 1 kg spool.
        assert [(r["spool_id"], r["weight_used"]) for r in results] == [(spool.id, 100.0)]

    @pytest.mark.asyncio
    async def test_without_the_row_there_is_nothing_to_charge(self, db_session, printer_factory):
        """The pre-fix behaviour, kept explicit: no session, no remain baseline,
        no attribution — which is what a restart used to leave behind."""
        from backend.app.services.usage_tracker import on_print_complete

        printer = await printer_factory()
        state = MagicMock()
        state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]}
        state.progress = 100
        state.tray_change_log = []
        pm = MagicMock()
        pm.get_status.return_value = state

        assert await on_print_complete(printer.id, {"status": "completed"}, pm, db_session) == []


class TestTheRestoredSessionIsTheRunningPrint:
    @pytest.mark.asyncio
    async def test_a_stale_row_can_be_told_apart_by_name(self, db_session, printer_factory):
        """The row and the printer disagreeing is how a leaked row is caught —
        the alternative is charging this print's filament to the last one's
        spools."""
        printer = await printer_factory()
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await persist_session(db_session, _session(printer.id, print_name="yesterday.3mf", started_at=yesterday))

        assert await get_persisted_print_name(db_session, printer.id) != "today.3mf"


class TestTheJournalSupersedesTheColumn:
    """m153: the events table is the tray log's home; the JSON column is a
    one-release read fallback for a print running across the upgrade."""

    async def _archive(self, db_session, printer, status="printing"):
        from backend.app.models.archive import PrintArchive

        a = PrintArchive(
            printer_id=printer.id,
            filename="bin.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="bin",
            status=status,
        )
        db_session.add(a)
        await db_session.commit()
        await db_session.refresh(a)
        return a

    @pytest.mark.asyncio
    async def test_print_start_seeds_a_start_event(self, db_session, printer_factory):
        from backend.app.models.print_usage_event import PrintUsageEvent

        printer = await printer_factory()
        archive = await self._archive(db_session, printer)

        await on_print_start(
            printer.id,
            {"subtask_name": "bin.3mf"},
            _printer_manager_with_ams(),
            db=db_session,
        )

        events = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
        assert [(e.event, e.global_tray_id, e.layer_num, e.archive_id) for e in events] == [("start", 0, 0, archive.id)]

    @pytest.mark.asyncio
    async def test_tray_change_event_freezes_the_assigned_spool(self, db_session, printer_factory):
        from backend.app.models.print_usage_event import PrintUsageEvent
        from backend.app.models.spool import Spool
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.services.usage_tracker import record_tray_change_event

        printer = await printer_factory()
        archive = await self._archive(db_session, printer)
        spool = Spool(material="PLA", label_weight=1000)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spool_id=spool.id))
        await db_session.commit()

        await record_tray_change_event(db_session, printer.id, 1, 250)

        events = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
        assert [(e.event, e.global_tray_id, e.layer_num, e.spool_id) for e in events] == [
            ("tray_change", 1, 250, spool.id)
        ]
        assert events[0].archive_id == archive.id

    @pytest.mark.asyncio
    async def test_tray_change_without_an_active_archive_is_dropped(self, db_session, printer_factory):
        from backend.app.models.print_usage_event import PrintUsageEvent
        from backend.app.services.usage_tracker import record_tray_change_event

        printer = await printer_factory()
        await record_tray_change_event(db_session, printer.id, 1, 10)
        assert (await db_session.execute(select(PrintUsageEvent))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_restore_prefers_journal_rows_over_the_legacy_column(self, db_session, printer_factory):
        from backend.app.services.usage_tracker import record_tray_change_event

        printer = await printer_factory()
        await self._archive(db_session, printer)
        # A row written by the OLD binary carries the legacy column…
        await persist_session(db_session, _session(printer.id), [(9, 9)])
        # …but this process has journal rows for the same print.
        await record_tray_change_event(db_session, printer.id, 0, 0)
        await record_tray_change_event(db_session, printer.id, 1, 120)

        log = await restore_session(db_session, printer.id)
        assert log == [[0, 0], [1, 120]]

    @pytest.mark.asyncio
    async def test_restore_falls_back_to_the_legacy_column(self, db_session, printer_factory):
        printer = await printer_factory()
        await self._archive(db_session, printer)
        await persist_session(db_session, _session(printer.id), [(0, 0), (1, 120)])

        log = await restore_session(db_session, printer.id)
        assert log == [[0, 0], [1, 120]]

    @pytest.mark.asyncio
    async def test_consecutive_duplicate_events_are_not_journaled_twice(self, db_session, printer_factory):
        from backend.app.models.print_usage_event import PrintUsageEvent
        from backend.app.services.usage_tracker import record_tray_change_event

        printer = await printer_factory()
        await self._archive(db_session, printer)
        await record_tray_change_event(db_session, printer.id, 0, 0)
        await record_tray_change_event(db_session, printer.id, 0, 0)

        events = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
        assert len(events) == 1
