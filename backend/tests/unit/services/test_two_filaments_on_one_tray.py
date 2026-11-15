"""Two slicer filaments of one file on the same tray are both charged (upstream 384903328, #3230).

A print sent from the slicer with two of its filaments mapped to ONE physical tray (our own dispatch
refuses such a mapping) was charged for the first only: the per-slot loop skipped a slot whose tray was
already charged. Slot 1 = 1.05 g and slot 2 = 2.13 g, both on AMS 0 tray 1: the spool got 1.05 g instead of
3.18 g, nobody was told, and the other 2.13 g were priced at the default rate. Spoolman's writer charges
both, so the two inventories disagreed.

Our accounting divides ONE file slot between trays when the feed changes (the tray chronology, the
journal). This is the reverse case -- two file slots, one tray, no change at all -- so none of that
engages: the fix is only that every file slot is charged its own grams exactly once.

- a slot listed twice (a multi-plate file whose plate is unknown lists it once per plate) is still charged
  once -- that is what the tray skip did by accident, and it is now said in so many words;
- a tray reached only by a positional guess that is already charged is still not charged again, and the
  grams are now reported as not recorded instead of dropped silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED, EVENT_START, KIND_MANUAL
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.usage_tracker import _active_sessions
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import (  # noqa: F401 — fixtures
    _clean_memory,
    history,
    journal,
    next_print_state,
    run_internal,
    session,
    stage,
)
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

TWO_ON_ONE = [
    {"slot_id": 1, "used_g": 1.05, "type": "PLA", "color": "#FF0000"},
    {"slot_id": 2, "used_g": 2.13, "type": "PLA", "color": "#FF0000"},
]
COMPLETED = {
    "status": "completed",
    "last_layer_num": 200,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100.0),
}
AMS = [{"id": 0, "tray": [{"id": n, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11} for n in range(2)]}]


def printer_state():
    return next_print_state([], ams=AMS)


async def _priced(db, stage_, *, spool_rate=20.0, default_rate="25", estimate=3.18):  # noqa: F811
    """Spool A costs 20 /kg; the farm's default rate is 25 /kg; the slicer's estimate for the print."""
    (await db.get(Spool, stage_.a)).cost_per_kg = spool_rate
    db.add(Settings(key="default_filament_cost", value=default_rate))
    (await db.get(PrintArchive, stage_.archive.id)).filament_used_grams = estimate
    await db.commit()


async def _rows(db):
    rows = (await db.execute(select(SpoolUsageHistory).order_by(SpoolUsageHistory.id))).scalars().all()
    return [(r.spool_id, r.weight_used, r.cost) for r in rows]


class TestBothFilamentsAreCharged:
    async def test_the_spool_gets_both_filaments_and_the_print_is_priced_from_it(self, db_session, stage):  # noqa: F811
        """Was 1.05 g on the spool, one row, and a cost of 0.07 (2.13 g at the default 25 /kg)."""
        await _priced(db_session, stage)
        _active_sessions[stage.printer.id] = session(
            stage.printer.id, spool_assignments={(0, 0): stage.a}, started_at=None, ams_mapping=[0, 0]
        )

        _notice, not_recorded = await run_internal(
            db_session, stage.printer, stage.archive, COMPLETED, printer_state(), usage=TWO_ON_ONE
        )

        spool = (
            await db_session.execute(select(Spool).where(Spool.id == stage.a).execution_options(populate_existing=True))
        ).scalar_one()
        assert round(spool.weight_used, 2) == 3.18
        # One row per file slot, as every other charge: the History view lists what each filament used,
        # and each row carries its own cost at the spool's rate.
        assert await _rows(db_session) == [(stage.a, 1.1, 0.02), (stage.a, 2.1, 0.04)]
        assert not_recorded == []
        archive = (
            await db_session.execute(
                select(PrintArchive)
                .where(PrintArchive.id == stage.archive.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert archive.cost == 0.06
        assert archive.filament_used_grams == 3.18

    async def test_a_runout_on_the_shared_tray_divides_each_filament_by_its_own_boundary(
        self,
        db_session,
        stage,  # noqa: F811
    ):
        """Reel A to layer 100 of 200, reel B after, in the same slot. Was {(A, 5.0), (B, 5.0)}: the
        second filament's 20 g were charged to nobody."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [
                (EVENT_START, None, 0, 0, stage.a),
                (EVENT_RUNOUT, KIND_MANUAL, 0, 100, stage.a),
                (EVENT_SPOOL_LOADED, None, 0, 100, stage.b),
            ],
        )
        _active_sessions[stage.printer.id] = session(
            stage.printer.id, spool_assignments={(0, 0): stage.a}, started_at=None, ams_mapping=[0, 0]
        )
        usage = [{"slot_id": 1, "used_g": 10.0, "type": "PLA"}, {"slot_id": 2, "used_g": 20.0, "type": "PLA"}]

        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, printer_state(), usage=usage)

        assert await history(db_session) == {(stage.a, 5.0), (stage.b, 5.0), (stage.a, 10.0), (stage.b, 10.0)}


class TestWhatTheTraySkipStillProtects:
    async def test_a_slot_listed_once_per_plate_is_charged_once(self, db_session, stage):  # noqa: F811
        """A multi-plate file whose plate is unknown lists a slot once per plate. Dropping the tray skip
        alone would charge every plate: 22 g for a 10 g print."""
        _active_sessions[stage.printer.id] = session(
            stage.printer.id, spool_assignments={(0, 0): stage.a}, started_at=None, ams_mapping=[0]
        )
        usage = [{"slot_id": 1, "used_g": 10.0, "type": "PLA"}, {"slot_id": 1, "used_g": 12.0, "type": "PLA"}]

        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, printer_state(), usage=usage)

        assert await history(db_session) == {(stage.a, 10.0)}

    async def test_a_guessed_tray_that_is_already_charged_is_not_charged_again_and_is_reported(
        self,
        db_session,
        stage,  # noqa: F811
    ):
        """The mapping names one slot; the second filament's tray is its POSITION among the loaded trays --
        a guess, and it lands on the tray the first filament was charged to. Was not charged and not
        reported; still not charged, and the 2.13 g are now named."""
        _active_sessions[stage.printer.id] = session(
            stage.printer.id, spool_assignments={(0, 1): stage.a}, started_at=None, ams_mapping=[1]
        )

        _notice, not_recorded = await run_internal(
            db_session, stage.printer, stage.archive, COMPLETED, printer_state(), usage=TWO_ON_ONE
        )

        assert await history(db_session) == {(stage.a, 1.1)}
        assert not_recorded == [(1, 2.13)]

    async def test_distinct_trays_are_charged_exactly_as_before(self, db_session, stage):  # noqa: F811
        """GUARD 1: the ordinary multi-colour print."""
        await _priced(db_session, stage)
        _active_sessions[stage.printer.id] = session(
            stage.printer.id, spool_assignments={(0, 0): stage.a, (0, 1): stage.b}, started_at=None, ams_mapping=[0, 1]
        )

        _notice, not_recorded = await run_internal(
            db_session, stage.printer, stage.archive, COMPLETED, printer_state(), usage=TWO_ON_ONE
        )

        # Reel B has no price of its own: the default 25 /kg, as before.
        assert await _rows(db_session) == [(stage.a, 1.1, 0.02), (stage.b, 2.1, 0.05)]
        assert not_recorded == []


class TestSpoolmanAlreadyDidThis:
    async def test_both_inventories_charge_the_same_grams_for_the_same_print(self):
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            archive_id=144,
            filament_usage=TWO_ON_ONE,
            layer_usage=None,
            filament_properties={},
            ams_trays={0: {"tray_uuid": "TRAY0", "tray_type": "PLA"}},
            slot_to_tray=[0, 0],
            tray_remain_start={},
        )
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(return_value={"id": 54, "filament": {}})
        client.use_spool = AsyncMock()
        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
            patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
            patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
            patch("backend.app.services.printer_manager.printer_manager", printer_state()),
            patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", AsyncMock()),
        ):
            await report_usage(printer_id=1, archive_id=144, completion=CompletionSnapshot.from_event(COMPLETED))

        charged = [(call.args[0], call.args[1]) for call in client.use_spool.await_args_list]
        assert charged == [(54, 1.05), (54, 2.13)]  # 3.18 g: what the built-in spool gets in the first test
