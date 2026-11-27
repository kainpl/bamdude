"""A print that stopped divides what it PRINTED between its reels — in both inventory writers.

Spec partial-print-layer-split r2 §6. The print stopped at layer 50 of 200; reel A fed it up
to layer 40, reel B from there. Whatever weight the writer determined for the stopped print
(10 g here), A printed 40 of the 50 printed layers and B 10: 8 g / 2 g. The writers used to
divide the early stretch by the whole plate (2 / 8), and the partial Spoolman writer did not
read a reel change inside one tray at all (B got all 10 g).

How far the print got is the completion EVENT's: by the time the writers run, the printer
may be reporting the next print. Every test below therefore gives the live printer state a
different print's figures (layer 3 of 100, 2 %) — a writer that read them would divide wrong.

Expected values are constants worked out by hand, never the helper's own answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.core.config import settings as app_settings
from backend.app.models.print_usage_event import (
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_START,
    EVENT_TRAY_CHANGE,
    KIND_AUTOSWITCH,
    KIND_MANUAL,
    PrintUsageEvent,
)
from backend.app.models.settings import Settings
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import spoolman_charge
from backend.app.services.usage_tracker import PrintSession, _active_sessions, on_print_complete
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.app.utils.usage_attribution import UNKNOWN_FEED
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db
from backend.tests.unit.services.test_usage_tracker_runout import _make_archive, _make_printer, _make_spool

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = pytest.mark.usefixtures("spoolman_charge_admitted")

#: The slicer's estimate for the slot; the stopped print used a quarter of it — 10 g.
ESTIMATE_G = 40.0


@pytest.fixture(autouse=True)
def _clean_memory():
    _active_sessions.clear()
    yield
    _active_sessions.clear()


def failed_at(layer, *, captured=49, total=200, progress=25.0, captured_progress=24.8):
    """The completion event of a print that stopped at ``layer`` (0: the firmware reset it)."""
    return {
        "status": "failed",
        "last_layer_num": captured,
        "last_progress": captured_progress,
        **terminal_snapshot_fields(layer_num=layer, total_layers=total, progress=progress),
    }


def next_print_state(tray_change_log, ams=None):
    """The printer as it reports at accounting time: already on someone else's print."""
    state = MagicMock()
    state.raw_data = {"ams": ams or []}
    state.progress = 2.0
    state.layer_num = 3
    state.total_layers = 100
    state.tray_now = 255
    state.last_loaded_tray = -1
    state.tray_change_log = list(tray_change_log)
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


async def journal(db_session, printer, archive, rows):
    """rows: (event, kind, tray, layer, spool_id)"""
    for event, kind, tray, layer, spool_id in rows:
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=layer,
                event=event,
                kind=kind,
                global_tray_id=tray,
                spool_id=spool_id,
                spoolman_spool_id=None,
            )
        )
    await db_session.commit()


def session(printer_id, **kw):
    args = {
        "printer_id": printer_id,
        "print_name": "test_print",
        "started_at": datetime.now(timezone.utc),
        "tray_remain_start": {},
        "tray_now_at_start": -1,
        "spool_assignments": {},
        "ams_mapping": [0],
    }
    args.update(kw)
    return PrintSession(**args)


def by_snapshot(stage):
    """A session whose start snapshot names tray 0's and tray 1's reels (A, B)."""
    return session(stage.printer.id, spool_assignments={(0, 0): stage.a, (0, 1): stage.b}, started_at=None)


async def history(db_session):
    rows = (await db_session.execute(select(SpoolUsageHistory).order_by(SpoolUsageHistory.id))).scalars().all()
    return {(r.spool_id, r.weight_used) for r in rows}


async def run_internal(db_session, printer, archive, data, pm, usage=None, layer_usage=None):
    """The internal writer's real completion entry point, the 3MF extraction faked."""
    usage = usage or [{"slot_id": 1, "used_g": ESTIMATE_G, "type": "PLA", "color": "#FF0000"}]
    not_recorded = AsyncMock()
    with (
        patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=usage),
        patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=layer_usage),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", not_recorded),
    ):
        await on_print_complete(printer.id, data, pm, db_session, archive_id=archive.id)
    reported = not_recorded.await_args.args[1] if not_recorded.await_args else []
    return not_recorded, sorted(reported)


@pytest.fixture
async def stage(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    # The runout zero point closes a reel at its label weight after the split; off, so the
    # assertions read the split alone.
    db_session.add(Settings(key="runout_zero_point_enabled", value="false"))
    printer = await _make_printer(db_session)
    archive = await _make_archive(db_session, printer, tmp_path)
    reel_a = await _make_spool(db_session)
    reel_b = await _make_spool(db_session)
    return SimpleNamespace(printer=printer, archive=archive, a=reel_a.id, b=reel_b.id)


class TestInternalWriter:
    async def test_a_backup_takeover_divides_the_printed_stretch(self, db_session, stage):
        """A01/A22: tray 0 (A) to layer 40, tray 1 (B) after; stopped at 50 of 200."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        await run_internal(db_session, stage.printer, stage.archive, failed_at(50), next_print_state([(0, 0), (1, 40)]))

        assert await history(db_session) == {(stage.a, 8.0), (stage.b, 2.0)}

    @pytest.mark.parametrize("terminal, expected", [(50, "8/2"), (0, "10/0")])
    async def test_the_terminal_layer_wins_and_the_captured_one_stands_in_for_a_reset(
        self, db_session, stage, terminal, expected
    ):
        """A04: terminal layer 50, captured 40 → split at 50; terminal reset to 0 → at 40, so B
        (which took over at 40) printed nothing of it."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            failed_at(terminal, captured=40),
            next_print_state([(0, 0), (1, 40)]),
        )

        charged = dict(await history(db_session))
        assert {"8/2": {stage.a: 8.0, stage.b: 2.0}, "10/0": {stage.a: 10.0}}[expected] == charged

    async def test_a_reel_changed_in_one_tray_divides_the_printed_stretch(self, db_session, stage):
        """A10: one tray — A runs out at 40, B loaded there; stopped at 50."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [
                (EVENT_START, None, 0, 0, stage.a),
                (EVENT_RUNOUT, KIND_MANUAL, 0, 40, stage.a),
                (EVENT_SPOOL_LOADED, None, 0, 40, stage.b),
            ],
        )
        _active_sessions[stage.printer.id] = session(stage.printer.id)

        await run_internal(db_session, stage.printer, stage.archive, failed_at(50), next_print_state([(0, 0)]))

        assert await history(db_session) == {(stage.a, 8.0), (stage.b, 2.0)}

    async def test_a_feed_nobody_saw_keeps_its_share_unrecorded(self, db_session, stage):
        """A11: tray 0 (A) to 40, then a feed nobody observed to the stop at 50."""
        await journal(db_session, stage.printer, stage.archive, [(EVENT_START, None, 0, 0, stage.a)])
        _active_sessions[stage.printer.id] = session(stage.printer.id)

        _notify, reported = await run_internal(
            db_session, stage.printer, stage.archive, failed_at(50), next_print_state([(0, 0), (UNKNOWN_FEED, 40)])
        )

        assert await history(db_session) == {(stage.a, 8.0)}
        assert reported == [(UNKNOWN_FEED, 2.0)]

    @pytest.mark.parametrize(
        "chronology, rows, label",
        [
            # One tray, two reels: the slot is known, the reels' shares are not.
            ([(0, 0)], [(EVENT_RUNOUT, KIND_MANUAL, 0, 40, "a"), (EVENT_SPOOL_LOADED, None, 0, 40, "b")], 0),
            # Two trays: no tray may be shown a figure nobody measured for it.
            ([(0, 0), (1, 40)], [(EVENT_TRAY_CHANGE, None, 1, 40, "b")], UNKNOWN_FEED),
        ],
    )
    async def test_with_no_printed_boundary_the_weight_is_not_recorded(
        self, db_session, stage, chronology, rows, label
    ):
        """A08/A20: no terminal layer, no captured one — only the plate count and the progress.
        The weight is never shared out equally, nor given to the last reel."""
        spool = {"a": stage.a, "b": stage.b}
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a)] + [(e, k, t, layer, spool[s]) for e, k, t, layer, s in rows],
        )
        _active_sessions[stage.printer.id] = session(stage.printer.id)

        notify, reported = await run_internal(
            db_session, stage.printer, stage.archive, failed_at(0, captured=0), next_print_state(chronology)
        )

        assert await history(db_session) == set()
        assert reported == [(label, 10.0)]
        notify.assert_awaited_once()

    @pytest.mark.parametrize("total", [0, None])
    @pytest.mark.parametrize(
        "chronology, rows, label",
        [
            ([(0, 0)], [(EVENT_RUNOUT, KIND_MANUAL, 0, 40, "a"), (EVENT_SPOOL_LOADED, None, 0, 40, "b")], 0),
            ([(0, 0), (1, 40)], [(EVENT_TRAY_CHANGE, None, 1, 40, "b")], UNKNOWN_FEED),
        ],
        ids=["one_tray", "two_trays"],
    )
    async def test_no_layer_at_all_still_sizes_the_weight_by_progress(
        self, db_session, stage, total, chronology, rows, label
    ):
        """PL-R1 parity: no terminal, captured or plate layer count — 25 % of 40 g is still a
        known 10 g, reported not recorded exactly as the Spoolman writer reports it."""
        spool = {"a": stage.a, "b": stage.b}
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a)] + [(e, k, t, layer, spool[s]) for e, k, t, layer, s in rows],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        notify, reported = await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            failed_at(0, captured=0, total=total, progress=25.0, captured_progress=25.0),
            next_print_state(chronology),
        )

        assert await history(db_session) == set()
        notify.assert_awaited_once()
        assert reported == [(label, 10.0)]

    async def test_no_layer_and_no_progress_invent_no_weight(self, db_session, stage):
        """PL-R1 control on the internal writer: nothing sizes the weight, nothing is written."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        notify, _reported = await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            failed_at(0, captured=0, total=0, progress=0, captured_progress=0),
            next_print_state([(0, 0), (1, 40)]),
        )

        assert await history(db_session) == set()
        notify.assert_not_awaited()

    async def test_one_reel_in_every_stretch_takes_the_whole_weight_without_a_boundary(self, db_session, stage):
        """A08 control: the same reel on both sides of the boundary — nothing to divide."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [
                (EVENT_START, None, 0, 0, stage.a),
                (EVENT_RUNOUT, KIND_MANUAL, 0, 40, stage.a),
                (EVENT_SPOOL_LOADED, None, 0, 40, stage.a),
            ],
        )
        _active_sessions[stage.printer.id] = session(stage.printer.id)

        await run_internal(
            db_session, stage.printer, stage.archive, failed_at(0, captured=0), next_print_state([(0, 0)])
        )

        assert await history(db_session) == {(stage.a, 10.0)}

    async def test_a_layer_estimated_from_the_progress_never_divides(self, db_session, stage):
        """A24: progress alone sizes the weight (10 g), but it says nothing about where the reel
        change sits relative to the stop."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = session(stage.printer.id)

        _notify, reported = await run_internal(
            db_session, stage.printer, stage.archive, failed_at(0, captured=0), next_print_state([(0, 0), (1, 40)])
        )

        assert await history(db_session) == set()
        assert reported == [(UNKNOWN_FEED, 10.0)]

    async def test_a_reel_that_printed_nothing_is_not_charged_by_the_remain_fallback(self, db_session, stage):
        """A21: the chronology, held only in memory, puts B in tray 1 at the stop itself; its
        share is 0 g, and the remain% drop of tray 1 must not charge it afterwards."""
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(0, 0): stage.a, (0, 1): stage.b},
            tray_remain_start={(0, 1): 80},
            started_at=None,
        )
        ams = [{"id": 0, "tray": [{"id": 0, "remain": 50}, {"id": 1, "remain": 70}]}]

        await run_internal(
            db_session, stage.printer, stage.archive, failed_at(50), next_print_state([(0, 0), (1, 50)], ams=ams)
        )

        assert await history(db_session) == {(stage.a, 10.0)}

    @pytest.mark.parametrize("terminal, expected", [(50, {"a": 8.0, "b": 7.0}), (0, {"b": 5.0})])
    async def test_the_purge_is_charged_apart_from_the_divided_weight(self, db_session, stage, terminal, expected):
        """A14: the AMS backup's purge (5 g) is the backup's whatever the division — at 50 it
        sits on B's 2 g; with no printed boundary the 10 g go unrecorded and the purge stays B's."""
        db_session.add(Settings(key="runout_purge_grams", value="5"))
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [
                (EVENT_START, None, 0, 0, stage.a),
                (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 40, stage.a),
                (EVENT_TRAY_CHANGE, None, 1, 40, stage.b),
            ],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            failed_at(terminal, captured=0),
            next_print_state([(0, 0), (1, 40)]),
        )

        spool = {"a": stage.a, "b": stage.b}
        assert await history(db_session) == {(spool[k], grams) for k, grams in expected.items()}

    async def test_unrecorded_weight_is_not_charged_again_by_the_remain_fallback(self, db_session, stage):
        """A16: both trays' remain% dropped during the print; the 10 g nobody could divide stay
        unrecorded — neither tray is charged by its drop afterwards."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)
        _active_sessions[stage.printer.id].tray_remain_start = {(0, 0): 90, (0, 1): 80}
        ams = [{"id": 0, "tray": [{"id": 0, "remain": 60}, {"id": 1, "remain": 70}]}]

        _notify, reported = await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            failed_at(0, captured=0),
            next_print_state([(0, 0), (1, 40)], ams=ams),
        )

        assert await history(db_session) == set()
        assert reported == [(UNKNOWN_FEED, 10.0)]


# ── Spoolman ───────────────────────────────────────────────────────────────────


def event(i, name, tray, layer, spoolman_id, kind=None):
    return SimpleNamespace(
        id=i,
        event=name,
        global_tray_id=tray,
        layer_num=layer,
        kind=kind,
        spool_id=None,
        spoolman_spool_id=spoolman_id,
        slot_occupied=True,
    )


#: Tray 0's reel ran out at 40 (A, Spoolman 11) and B (Spoolman 12) was loaded there.
REEL_CHANGE_IN_TRAY_0 = [
    event(1, EVENT_START, 0, 0, 11),
    event(2, EVENT_RUNOUT, 0, 40, 11, kind=KIND_MANUAL),
    event(3, EVENT_SPOOL_LOADED, 0, 40, 12),
]

#: Tray 0 (A, 11) to layer 40, then the AMS backup in tray 1 (B, 12).
BACKUP_TAKEOVER = [event(1, EVENT_START, 0, 0, 11), event(2, EVENT_TRAY_CHANGE, 1, 40, 12)]


async def run_spoolman(data, *, usage, journal_events, tray_change_log, ams_trays=None, slot_to_tray=None):
    """The partial Spoolman writer's real entry point, Spoolman and the database faked."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=usage,
        layer_usage=None,
        filament_properties={},
        ams_trays=ams_trays or {0: {"tray_uuid": "TRAY0", "tray_type": "PLA"}, 1: {"tray_uuid": "TRAY1"}},
        slot_to_tray=slot_to_tray or [0],
        tray_remain_start={},
    )
    # At the stop tray 0 holds whatever reel it held last — B after a change in that tray — but
    # a reel the journal froze is charged by its id, never by the tag the tray shows now.
    reels = {"TRAY0": {"id": 11, "filament": {}}, "TRAY1": {"id": 12, "filament": {}}, "TRAY2": {"id": 21}}
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: reels.get(tag))
    client.use_spool = AsyncMock()
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=journal_events)),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", next_print_state(tray_change_log)),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            last_layer_num=data["last_layer_num"],
            last_progress=data["last_progress"],
            completion=CompletionSnapshot.from_event(data),
        )
    charged: dict[int, float] = {}
    for call in client.use_spool.await_args_list:
        charged[call.args[0]] = round(charged.get(call.args[0], 0.0) + call.args[1], 2)
    return charged, notify


ONE_SLOT = [{"slot_id": 1, "used_g": ESTIMATE_G}]


class TestSpoolmanWriter:
    async def test_a_backup_takeover_divides_the_printed_stretch(self):
        """A01/A22: Spoolman's partial weight is 40 g × 50/200 = 10 g — then 8 / 2."""
        charged, notify = await run_spoolman(
            failed_at(50), usage=ONE_SLOT, journal_events=BACKUP_TAKEOVER, tray_change_log=[(0, 0), (1, 40)]
        )
        assert charged == {11: 8.0, 12: 2.0}
        notify.assert_not_awaited()

    @pytest.mark.parametrize("terminal, expected", [(50, {11: 8.0, 12: 2.0}), (0, {11: 8.0})])
    async def test_the_terminal_layer_wins_and_the_captured_one_stands_in_for_a_reset(self, terminal, expected):
        """A04: at 50 → 8/2 of 10 g; reset to 0 → the captured 40 sizes the weight (8 g) and B,
        which took over at 40, printed none of it."""
        charged, _notify = await run_spoolman(
            failed_at(terminal, captured=40),
            usage=ONE_SLOT,
            journal_events=BACKUP_TAKEOVER,
            tray_change_log=[(0, 0), (1, 40)],
        )
        assert charged == expected

    async def test_a_reel_changed_in_one_tray_divides_the_printed_stretch(self):
        """A10: the partial writer reads the tray's own reel change, as completion does — and the
        mapping charges nothing on top."""
        charged, _notify = await run_spoolman(
            failed_at(50), usage=ONE_SLOT, journal_events=REEL_CHANGE_IN_TRAY_0, tray_change_log=[(0, 0)]
        )
        assert charged == {11: 8.0, 12: 2.0}

    async def test_a_multicolour_print_splits_only_the_slot_whose_reel_changed(self):
        """A19: slot 1 (tray 0) had its reel changed at 40; slot 2 (tray 2) did not. Each slot
        is 40 g × 50/200 = 10 g: slot 1 → 8 / 2, slot 2 → its mapped reel, once."""
        charged, _notify = await run_spoolman(
            failed_at(50),
            usage=[{"slot_id": 1, "used_g": ESTIMATE_G}, {"slot_id": 2, "used_g": ESTIMATE_G}],
            journal_events=REEL_CHANGE_IN_TRAY_0,
            tray_change_log=[(0, 0), (2, 10), (0, 11), (2, 30)],
            ams_trays={0: {"tray_uuid": "TRAY0"}, 2: {"tray_uuid": "TRAY2"}},
            slot_to_tray=[0, 2],
        )
        assert charged == {11: 8.0, 12: 2.0, 21: 10.0}

    @pytest.mark.parametrize(
        "events, chronology, label",
        [(REEL_CHANGE_IN_TRAY_0, [(0, 0)], 0), (BACKUP_TAKEOVER, [(0, 0), (1, 40)], UNKNOWN_FEED)],
        ids=["one_tray", "two_trays"],
    )
    async def test_with_no_printed_boundary_the_weight_is_not_recorded(self, events, chronology, label):
        """A08/A20/A24: no terminal or captured layer — the layer estimated from the progress
        (25 % of 200) sizes the weight, 10 g, but never divides it."""
        charged, notify = await run_spoolman(
            failed_at(0, captured=0), usage=ONE_SLOT, journal_events=events, tray_change_log=chronology
        )
        assert charged == {}
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == [(label, 10.0)]

    @pytest.mark.parametrize("total", [0, None])
    @pytest.mark.parametrize(
        "events, chronology, label",
        [(REEL_CHANGE_IN_TRAY_0, [(0, 0)], 0), (BACKUP_TAKEOVER, [(0, 0), (1, 40)], UNKNOWN_FEED)],
        ids=["one_tray", "two_trays"],
    )
    async def test_no_layer_at_all_still_sizes_the_weight_by_progress(self, total, events, chronology, label):
        """PL-R1 (Codex r1): no terminal, captured or plate layer count — but 25 % of the 40 g
        estimate is a known 10 g. An unknown boundary is no unknown weight: the 10 g are reported
        not recorded, never dropped without a word."""
        data = failed_at(0, captured=0, total=total, progress=25.0, captured_progress=25.0)
        charged, notify = await run_spoolman(data, usage=ONE_SLOT, journal_events=events, tray_change_log=chronology)
        assert charged == {}
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == [(label, 10.0)]

    async def test_no_layer_at_all_charges_a_single_reel_its_progress_share(self):
        """PL-R1 control: one reel fed the print — it takes the 10 g with no boundary needed."""
        charged, notify = await run_spoolman(
            failed_at(0, captured=0, total=0, progress=25.0),
            usage=ONE_SLOT,
            journal_events=[REEL_CHANGE_IN_TRAY_0[0]],
            tray_change_log=[(0, 0)],
        )
        assert charged == {11: 10.0}
        notify.assert_not_awaited()

    async def test_no_layer_and_no_progress_invent_no_weight(self):
        """PL-R1 control: neither a layer nor a progress — nothing to size the weight by."""
        charged, notify = await run_spoolman(
            failed_at(0, captured=0, total=0, progress=0, captured_progress=0),
            usage=ONE_SLOT,
            journal_events=REEL_CHANGE_IN_TRAY_0,
            tray_change_log=[(0, 0)],
        )
        assert charged == {}
        notify.assert_not_awaited()

    async def test_a_captured_layer_without_a_plate_count_still_divides(self):
        """PL-R1 control: captured 50, L reset — 10 g by progress, divided at 50 → 8 / 2."""
        charged, notify = await run_spoolman(
            failed_at(0, captured=50, total=0, progress=25.0),
            usage=ONE_SLOT,
            journal_events=REEL_CHANGE_IN_TRAY_0,
            tray_change_log=[(0, 0)],
        )
        assert charged == {11: 8.0, 12: 2.0}
        notify.assert_not_awaited()

    async def test_every_branch_reports_in_one_notice(self):
        """A20: the journal split's unrecorded reel and a mapped slot with no Spoolman spool —
        one notice for the report."""
        no_reel = [
            event(1, EVENT_START, 0, 0, 11),
            event(2, EVENT_RUNOUT, 0, 40, 11, kind=KIND_MANUAL),
            event(3, EVENT_SPOOL_LOADED, 0, 40, None),
        ]
        with patch("backend.app.services.spoolman_tracking._resolve_bound_spool", AsyncMock(return_value=None)):
            charged, notify = await run_spoolman(
                failed_at(50),
                usage=[{"slot_id": 1, "used_g": ESTIMATE_G}, {"slot_id": 2, "used_g": ESTIMATE_G}],
                journal_events=no_reel,
                tray_change_log=[(0, 0)],
                ams_trays={0: {"tray_uuid": "TRAY0"}, 3: {"tray_uuid": "NOBODY"}},
                slot_to_tray=[0, 3],
            )
        assert charged == {11: 8.0}
        notify.assert_awaited_once()
        assert sorted(notify.await_args.args[1]) == [(0, 2.0), (3, 10.0)]


# ── The completion event carries the figures (O5, A22) ─────────────────────────


class TestTheEventCarriesTheFigures:
    def _client(self):
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
        client.on_print_start = lambda data: None
        return client

    def _print_to_layer(self, client, layer):
        for n in range(1, layer + 1):
            client._process_message(
                {
                    "print": {
                        "gcode_state": "RUNNING",
                        "gcode_file": "/data/Metadata/a.gcode",
                        "subtask_name": "A",
                        "layer_num": n,
                        "total_layer_num": 200,
                        "mc_percent": n // 2,
                    }
                }
            )

    @pytest.mark.parametrize("terminal_layer", [50, 0])
    def test_the_terminal_frame_is_copied_and_outlives_the_next_print(self, terminal_layer):
        """A22: the event holds A's scalars at its terminal frame; B starting afterwards and
        rewriting the live state changes nothing in it."""
        client = self._client()
        events: list[dict] = []
        client.on_print_complete = events.append
        self._print_to_layer(client, 50)

        client._process_message(
            {"print": {"gcode_state": "FAILED", "gcode_file": "/data/Metadata/a.gcode", "layer_num": terminal_layer}}
        )
        for frame in (
            {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/b.gcode", "subtask_name": "B"},
            {"layer_num": 3, "total_layer_num": 100, "mc_percent": 2},
        ):
            client._process_message({"print": frame})

        (event,) = events
        snapshot = CompletionSnapshot.from_event(event)
        assert (snapshot.layer_num, snapshot.total_layers, snapshot.progress) == (
            terminal_layer or None,
            200,
            25.0,
        )
        assert snapshot.captured_layer == 50  # the last positive value before the terminal report
        assert (client.state.layer_num, client.state.total_layers) == (3, 100)

    def test_a_deferred_terminal_replays_its_own_figures(self):
        """A22: both replays (binding and timeout) hand on ``pending.data``."""
        from backend.app.services.print_run_binding import (
            begin_print_start_resolution,
            defer_matching_terminal_during_start,
            take_pending_terminal,
        )
        from backend.app.services.printer_manager import PrinterManager

        manager = PrinterManager()
        sequence = begin_print_start_resolution(manager, 7, {"subtask_id": "92"})
        data = {"status": "failed", "subtask_id": "92", "last_layer_num": 49, **failed_at(50)}
        defer_matching_terminal_during_start(manager, 7, data)
        data.update(terminal_snapshot_fields(layer_num=3, total_layers=100, progress=2.0))

        pending = take_pending_terminal(manager, 7, sequence)

        replayed = CompletionSnapshot.from_event({**pending.data, "_bamdude_pending_terminal_timeout": True})
        assert (replayed.layer_num, replayed.total_layers, replayed.progress) == (50, 200, 25.0)

    def test_main_hands_the_event_to_both_spoolman_writers(self):
        """O2: completed Spoolman divides by the event too — the captured layer included."""
        import ast
        from pathlib import Path

        import backend.app.main as main_module

        tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
        calls = {
            node.func.id: {kw.arg for kw in node.keywords}
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("_report_spoolman_usage", "_cleanup_spoolman_tracking")
        }
        assert set(calls) == {"_report_spoolman_usage", "_cleanup_spoolman_tracking"}
        assert all("completion" in keywords for keywords in calls.values())


class TestCompletedParity:
    """A18: a completed print whose plate count the firmware reset divides by the captured
    layer in BOTH writers — the same grams for the same event."""

    COMPLETED_AFTER_RESET = {
        "status": "completed",
        "last_layer_num": 100,
        "last_progress": 99.0,
        **terminal_snapshot_fields(layer_num=0, total_layers=0, progress=0),
    }

    async def test_internal(self, db_session, stage):
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        await run_internal(
            db_session, stage.printer, stage.archive, self.COMPLETED_AFTER_RESET, next_print_state([(0, 0), (1, 40)])
        )

        # 40 g over the 100 layers the captured hint says were printed: 40/100 and 60/100.
        assert await history(db_session) == {(stage.a, 16.0), (stage.b, 24.0)}

    async def test_spoolman(self):
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=ONE_SLOT,
            ams_trays={0: {"tray_uuid": "TRAY0"}, 1: {"tray_uuid": "TRAY1"}},
            slot_to_tray=[0],
            tray_remain_start={},
            layer_usage={},
            filament_properties={},
            archive_id=143,
        )
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: {"TRAY0": {"id": 11}, "TRAY1": {"id": 12}}[tag])
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
            patch("backend.app.services.printer_manager.printer_manager", next_print_state([(0, 0), (1, 40)])),
        ):
            await report_usage(
                printer_id=1, archive_id=143, completion=CompletionSnapshot.from_event(self.COMPLETED_AFTER_RESET)
            )

        assert {c.args[0]: c.args[1] for c in client.use_spool.await_args_list} == {11: 16.0, 12: 24.0}


# ── Which trays a split takes from the remain% fallback (R9) ───────────────────


class TestFences:
    async def test_spoolman_claims_every_stretchs_tray_and_flags_one_it_cannot_name(self):
        """Every tray of the slot's stretches is the split's, whatever its share; a stretch no
        boundary names (before the first one) is flagged so the caller fences every tray — as
        the internal writer does."""
        from backend.app.services.spoolman_tracking import _report_spool_usage_split_by_tray_changes
        from backend.app.utils.tray_split import PrintedBound

        stopped_at_50 = PrintedBound(completed=False, layer=50, source="current", total_layers=200)
        no_boundary = PrintedBound(completed=False, layer=None, source="none", total_layers=200)
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(return_value={"id": 11, "filament": {}})
        client.use_spool = AsyncMock()

        async def handled(chronology, bound):
            _updated, trays = await _report_spool_usage_split_by_tray_changes(
                client,
                [{"slot_id": 1, "used_g": 10.0}],
                chronology,
                {0: {"tray_uuid": "TRAY0"}, 1: {"tray_uuid": "TRAY1"}},
                None,
                bound,
                "test",
                "SERIAL",
                printer_id=1,
                journal_events=[],
                uncharged_out=[],
            )
            return trays

        assert await handled([(0, 0), (1, 50)], stopped_at_50) == {0, 1}  # B's share is 0 g
        assert await handled([(0, 0), (1, 40)], no_boundary) == {0, 1}  # both named: no wider fence
        assert UNKNOWN_FEED in await handled([(0, 10), (1, 40)], stopped_at_50)  # layers 0–10: nobody's

    async def test_a_purge_nobody_can_name_closes_every_tray_to_the_remain_fallback(self, db_session, stage):
        """The backup the journal names for an autoswitch is a feed nobody saw; its purge is
        reported not recorded, and no tray's remain% drop may charge it afterwards (here an
        unrelated tray 3 dropped 10 % during the print)."""
        db_session.add(Settings(key="runout_purge_grams", value="5"))
        other = (await _make_spool(db_session)).id
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [
                (EVENT_START, None, 0, 0, stage.a),
                (EVENT_TRAY_CHANGE, None, 2, 10, stage.b),
                (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 40, stage.a),
                (EVENT_TRAY_CHANGE, None, UNKNOWN_FEED, 40, None),
                (EVENT_TRAY_CHANGE, None, 0, 40, stage.a),
            ],
        )
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(0, 0): stage.a, (0, 2): stage.b, (0, 3): other},
            tray_remain_start={(0, 3): 80},
            started_at=None,
        )
        ams = [{"id": 0, "tray": [{"id": 3, "remain": 70}]}]
        data = {"status": "completed", **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100)}

        _notify, reported = await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            data,
            next_print_state([(0, 0), (2, 10), (UNKNOWN_FEED, 40), (0, 40)], ams=ams),
        )

        assert other not in {spool for spool, _grams in await history(db_session)}
        assert (UNKNOWN_FEED, 5.0) in reported


# ── What an event without the figures still knows (A17, A24) ──────────────────

#: One unit of extrusion per layer for slot 1 over a 200-layer plate.
SLOT_TIMELINE = {layer: {0: float(layer)} for layer in range(201)}


class TestEventsWithoutTheFigures:
    async def test_a_completed_print_with_its_slots_gcode_divides_without_any_layer(self, db_session, stage):
        """A17: an event that carries no layer at all (a reconcile completion) still divides
        a completed print by the slot's own timeline: 40 g, reel B from layer 40 of 200."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        notify, _reported = await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            {"status": "completed"},
            next_print_state([(0, 0), (1, 40)]),
            layer_usage=SLOT_TIMELINE,
        )

        assert await history(db_session) == {(stage.a, 8.0), (stage.b, 32.0)}
        notify.assert_not_awaited()

    async def test_spoolman_divides_a_completed_print_by_the_slots_gcode_without_any_layer(self):
        """A17 on Spoolman's completion writer."""
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=ONE_SLOT,
            ams_trays={0: {"tray_uuid": "TRAY0"}, 1: {"tray_uuid": "TRAY1"}},
            slot_to_tray=[0],
            tray_remain_start={},
            layer_usage={str(layer): {"0": mm} for layer, mm in ((k, v[0]) for k, v in SLOT_TIMELINE.items())},
            filament_properties={},
            archive_id=143,
        )
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: {"TRAY0": {"id": 11}, "TRAY1": {"id": 12}}[tag])
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
            patch("backend.app.services.printer_manager.printer_manager", next_print_state([(0, 0), (1, 40)])),
        ):
            await report_usage(printer_id=1, archive_id=143, completion=CompletionSnapshot.from_event({}))

        assert {c.args[0]: c.args[1] for c in client.use_spool.await_args_list} == {11: 8.0, 12: 32.0}

    async def test_an_event_of_the_old_shape_uses_its_captured_layer_never_the_next_print(self, db_session, stage):
        """A24: an event without the terminal figures knows its captured layer (40) and progress
        (25 %); the printer meanwhile reports another print's layer 3 of 100 — never read. B took
        over at 40, so it printed nothing of the 10 g."""
        await journal(
            db_session,
            stage.printer,
            stage.archive,
            [(EVENT_START, None, 0, 0, stage.a), (EVENT_TRAY_CHANGE, None, 1, 40, stage.b)],
        )
        _active_sessions[stage.printer.id] = by_snapshot(stage)

        await run_internal(
            db_session,
            stage.printer,
            stage.archive,
            {"status": "failed", "last_layer_num": 40, "last_progress": 25.0},
            next_print_state([(0, 0), (1, 40)]),
        )

        assert await history(db_session) == {(stage.a, 10.0)}

    async def test_spoolman_with_an_event_of_the_old_shape(self):
        """A24 on the partial Spoolman writer: with no plate layer count in the event the weight
        is the captured progress (40 g × 25 % = 10 g), and the captured layer 40 bounds the
        split — the same answer as the internal writer, not the next print's layer 3 of 100."""
        charged, _notify = await run_spoolman(
            {"status": "failed", "last_layer_num": 40, "last_progress": 25.0},
            usage=ONE_SLOT,
            journal_events=BACKUP_TAKEOVER,
            tray_change_log=[(0, 0), (1, 40)],
        )
        assert charged == {11: 10.0}

    async def test_the_timeout_replay_hands_the_writers_the_deferred_events_figures(self, monkeypatch):
        """A22, driven through ``main``'s real timeout replay."""
        import backend.app.main as main_module
        from backend.app.services.print_run_binding import (
            begin_print_start_resolution,
            defer_matching_terminal_during_start,
            end_print_start_resolution,
        )

        monkeypatch.setattr(main_module, "_PENDING_TERMINAL_TIMEOUT_SECONDS", 0)
        completed = AsyncMock()
        monkeypatch.setattr(main_module, "on_print_complete", completed)
        manager = main_module.printer_manager
        sequence = begin_print_start_resolution(manager, 9071, {"subtask_id": "92"})
        try:
            defer_matching_terminal_during_start(manager, 9071, {"subtask_id": "92", **failed_at(50)})
            await main_module._replay_expired_pending_terminal(9071, sequence)
        finally:
            end_print_start_resolution(manager, 9071, sequence)

        (printer_id, data), _kw = completed.await_args
        snapshot = CompletionSnapshot.from_event(data)
        assert (printer_id, snapshot.layer_num, snapshot.total_layers, snapshot.progress) == (9071, 50, 200, 25.0)
