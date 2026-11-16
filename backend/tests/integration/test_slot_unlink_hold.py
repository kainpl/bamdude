"""One "empty" report does not take a spool out of an idle slot (owner decision Р8; upstream 3497cf0d4).

An idle printer was seen to report a whole AMS unit empty for ONE push. Four
assignments were deleted, and nothing restores the assignment of a reel without
a tag. The unlink of "presence bit off, while idle" now waits ``HOLD_SECONDS``
and needs a second AMS report that still says so.

Accounting (GUARD 1): the hold decides only whether an idle slot keeps its row.
The last class books a 300 g print on a reel with 200 g used — once with no
glitch at all, once after a one-push "empty" — and both end at 500 g with the
same single history row. Before the hold, the glitch case ended at 200 g: the
slot had lost its spool and the print was charged to nobody.
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
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import slot_unlink_hold, usage_tracker
from backend.app.services.print_file_analysis import PrintFileAnalysis

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    slot_unlink_hold._holds.clear()
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    clock = Clock()
    monkeypatch.setattr(slot_unlink_hold, "_now", clock)
    # The re-check task is exercised on its own below; here the pushes are the test's.
    monkeypatch.setattr(slot_unlink_hold, "schedule_recheck", lambda *_a, **_k: None)
    yield clock
    slot_unlink_hold._holds.clear()
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)
    monkeypatch.setattr("backend.app.core.database.async_session", _session_ctx)


async def _unit_of_four(db, printer_factory):
    """Four untagged reels on AMS 0 — the #3186 report."""
    printer = await printer_factory()
    spools = []
    for tray_id in range(4):
        spool = Spool(material="PETG", rgba="858585FF", label_weight=1000, weight_used=200, cost_per_kg=20.0)
        db.add(spool)
        await db.commit()
        await db.refresh(spool)
        db.add(
            SpoolAssignment(
                spool_id=spool.id,
                printer_id=printer.id,
                ams_id=0,
                tray_id=tray_id,
                fingerprint_color="858585FF",
                fingerprint_type="PETG",
            )
        )
        spools.append(spool)
    await db.commit()
    return printer, spools


def _loaded(tray_id: int, **extra) -> dict:
    return {"id": tray_id, "exists": True, "state": 11, "tray_type": "PETG", "tray_color": "858585FF", **extra}


def _empty(tray_id: int) -> dict:
    """What ``apply_tray_exist_bits`` leaves of a slot whose presence bit reads 0."""
    return {"id": tray_id, "exists": False, "state": 9, "tray_type": "", "tray_color": ""}


def _occupied_blank(tray_id: int) -> dict:
    """Presence bit on, nothing readable: a reel the AMS cannot identify."""
    return {"id": tray_id, "exists": True, "state": 9, "tray_type": "", "tray_color": ""}


class Reports:
    """Feeds AMS reports through the real callback, each stamped as a new arrival."""

    def __init__(self, printer_id: int, clock: Clock):
        self.printer_id = printer_id
        self.clock = clock
        self.generation = 1
        self.stamp = 0.0
        self.broadcast = AsyncMock()

    async def push(self, trays: list[dict], *, state: str = "IDLE", new_report: bool = True) -> None:
        if new_report:
            self.stamp += 1.0
        ams = [{"id": 0, "tray": trays}]
        status = SimpleNamespace(
            state=state,
            raw_data={"ams": ams},
            connected=True,
            connection_generation=self.generation,
            last_ams_update=self.stamp,
            tray_now=255,
            last_loaded_tray=-1,
            tray_change_log=[],
        )
        with (
            patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
            patch.object(main_module.printer_manager, "get_status", return_value=status),
            patch.object(main_module.printer_manager, "get_client", return_value=None),
            patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
            patch.object(main_module.ws_manager, "broadcast", new=self.broadcast),
            patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
        ):
            await main_module.on_ams_change(self.printer_id, ams)

    def unlinked(self) -> list[int]:
        return sorted(
            call.args[0]["tray_id"]
            for call in self.broadcast.await_args_list
            if call.args[0].get("type") == "spool_assignment_changed"
        )


async def _assigned_trays(db, printer_id: int) -> list[int]:
    rows = (
        await db.execute(
            select(SpoolAssignment.tray_id)
            .where(SpoolAssignment.printer_id == printer_id)
            .order_by(SpoolAssignment.tray_id)
        )
    ).all()
    return [row[0] for row in rows]


EVERYTHING_EMPTY = [_empty(t) for t in range(4)]
EVERYTHING_LOADED = [_loaded(t) for t in range(4)]


class TestOneEmptyReport:
    async def test_a_whole_unit_blank_for_one_push_keeps_all_four_assignments(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)

        await reports.push(EVERYTHING_EMPTY)
        clock.now += 2
        await reports.push(EVERYTHING_LOADED)

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
        assert reports.unlinked() == []
        assert not slot_unlink_hold.has_open(printer.id)  # the reels are back: nothing is left waiting

    async def test_a_second_empty_report_inside_the_window_still_unlinks_nothing(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)

        await reports.push(EVERYTHING_EMPTY)
        clock.now += slot_unlink_hold.HOLD_SECONDS - 1
        await reports.push(EVERYTHING_EMPTY)

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]


class TestAReelReallyTakenOut:
    async def test_it_is_unlinked_once_the_slot_stayed_empty_for_the_window(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out)
        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out)

        assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]
        assert reports.unlinked() == [0]

    async def test_time_alone_confirms_nothing_without_a_second_report(
        self, db_session, main_db, printer_factory, clock
    ):
        """The window ended but the printer has said nothing new: the first report is all there is."""
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out)
        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out, new_report=False)

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]

    async def test_a_reconnect_restarts_the_clock(self, db_session, main_db, printer_factory, clock):
        """A cached state after a reconnect is no second observation."""
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out)
        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        reports.generation += 1
        await reports.push(one_out)
        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]

        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out)
        assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]

    async def test_an_unreadable_reel_put_in_meanwhile_does_not_inherit_the_assignment(
        self, db_session, main_db, printer_factory, clock
    ):
        """Empty first, then occupied-but-blank: a different reel may be in there. The clock keeps running."""
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        rest = [_loaded(1), _loaded(2), _loaded(3)]

        await reports.push([_empty(0), *rest])
        clock.now += 5
        await reports.push([_occupied_blank(0), *rest])
        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]

        clock.now += slot_unlink_hold.HOLD_SECONDS
        await reports.push([_occupied_blank(0), *rest])
        assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]

    async def test_an_occupied_blank_slot_that_was_never_reported_empty_keeps_its_spool(
        self, db_session, main_db, printer_factory, clock
    ):
        """Unchanged (upstream #3100): a reel the AMS cannot identify is not a reel taken out."""
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        blank = [_occupied_blank(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(blank)
        clock.now += slot_unlink_hold.HOLD_SECONDS * 3
        await reports.push(blank)

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]


class TestWhatIsNotHeld:
    async def test_another_filament_in_the_slot_unlinks_at_once(self, db_session, main_db, printer_factory, clock):
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)

        await reports.push([_loaded(0, tray_type="PLA", tray_color="00FF00FF"), _loaded(1), _loaded(2), _loaded(3)])

        assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]

    async def test_a_slot_that_empties_mid_print_is_a_runout_and_no_hold_is_opened(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out, state="RUNNING")
        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out, state="RUNNING")

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
        assert not slot_unlink_hold.has_open(printer.id)

    async def test_a_print_that_starts_inside_the_window_drops_the_hold(
        self, db_session, main_db, printer_factory, clock
    ):
        """The runout rule is asked first, always: a held slot of a printing printer is not unlinked."""
        printer, _spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out)
        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out, state="RUNNING")

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
        assert not slot_unlink_hold.has_open(printer.id)

    async def test_a_new_assignment_made_inside_the_window_gets_its_own_clock(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        one_out = [_empty(0), _loaded(1), _loaded(2), _loaded(3)]

        await reports.push(one_out)
        # The operator pre-assigns another reel to the empty slot (a new row).
        old = (
            await db_session.execute(
                select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id, SpoolAssignment.tray_id == 0)
            )
        ).scalar_one()
        await db_session.delete(old)
        newcomer = Spool(material="PETG", rgba="858585FF", label_weight=1000, weight_used=0)
        db_session.add(newcomer)
        await db_session.commit()
        db_session.add(
            SpoolAssignment(
                spool_id=newcomer.id,
                printer_id=printer.id,
                ams_id=0,
                tray_id=0,
                fingerprint_color="858585FF",
                fingerprint_type="PETG",
            )
        )
        await db_session.commit()

        clock.now += slot_unlink_hold.HOLD_SECONDS + 1
        await reports.push(one_out)

        assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
        kept = (
            await db_session.execute(
                select(SpoolAssignment.spool_id).where(
                    SpoolAssignment.printer_id == printer.id, SpoolAssignment.tray_id == 0
                )
            )
        ).scalar_one()
        assert kept == newcomer.id


ANALYSIS = PrintFileAnalysis(
    filament_usage=[{"slot_id": 1, "used_g": 300.0, "type": "PETG", "color": "#858585"}],
    layer_usage=None,
    filament_properties={},
)


async def _book_a_300g_print_from_tray_0(db, printer, spool) -> tuple[float, list[tuple[str, float]]]:
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
        raw_data={"ams": [{"id": 0, "tray": EVERYTHING_LOADED}]},
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
    """One reel, 200 g used, a print of 300 g from AMS0-T0. Truth: 500 g."""

    async def test_a_normal_print_is_charged_exactly_as_before(self, db_session, main_db, printer_factory, clock):
        printer, spools = await _unit_of_four(db_session, printer_factory)

        assert await _book_a_300g_print_from_tray_0(db_session, printer, spools[0]) == (500.0, [("completed", 300.0)])

    async def test_the_print_after_a_one_push_glitch_is_charged_to_the_same_reel(
        self, db_session, main_db, printer_factory, clock
    ):
        printer, spools = await _unit_of_four(db_session, printer_factory)
        reports = Reports(printer.id, clock)
        await reports.push(EVERYTHING_EMPTY)
        clock.now += 2
        await reports.push(EVERYTHING_LOADED)

        # Was (200.0, []): the glitch had unlinked the slot and the print charged nobody.
        assert await _book_a_300g_print_from_tray_0(db_session, printer, spools[0]) == (500.0, [("completed", 300.0)])


class TestTheReCheck:
    """A slot that stays empty produces no AMS change of its own: the window has to end by itself."""

    async def test_it_asks_again_when_the_window_ends_and_stops_once_nothing_is_held(self, monkeypatch):
        slot_unlink_hold._holds.clear()
        asked: list[str] = []
        sleeps: list[float] = []

        async def instant(seconds: float) -> None:
            sleeps.append(seconds)

        async def run() -> None:
            asked.append("again")
            slot_unlink_hold.keep_only(7, set())  # the pass unlinked it

        monkeypatch.setattr(asyncio, "sleep", instant)
        slot_unlink_hold.offer((7, 0, 0), row_id=1)
        await slot_unlink_hold._recheck(7, run)

        assert asked == ["again"]
        assert sleeps == [slot_unlink_hold.HOLD_SECONDS + 1]

    async def test_it_gives_up_after_its_attempts(self, monkeypatch):
        slot_unlink_hold._holds.clear()
        asked: list[str] = []

        async def instant(_seconds: float) -> None:
            return None

        async def run() -> None:
            asked.append("again")

        monkeypatch.setattr(asyncio, "sleep", instant)
        slot_unlink_hold.offer((7, 0, 0), row_id=1)
        await slot_unlink_hold._recheck(7, run)

        assert len(asked) == slot_unlink_hold.RECHECKS

    async def test_nothing_held_asks_nothing(self, monkeypatch):
        slot_unlink_hold._holds.clear()
        asked: list[str] = []

        async def instant(_seconds: float) -> None:
            return None

        async def run() -> None:
            asked.append("again")

        monkeypatch.setattr(asyncio, "sleep", instant)
        await slot_unlink_hold._recheck(7, run)

        assert asked == []
