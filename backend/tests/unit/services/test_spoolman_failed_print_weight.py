"""A stopped print weighs the same in both inventory backends (upstream a8dd164c7, #3317).

The internal writer charges a stopped print ``slicer estimate x the slot's progress
along its own G-code timeline``. The Spoolman writer charged the ABSOLUTE G-code
grams at the stop layer — a figure the plate G-code cannot give: flush / purge on
a filament change runs inside firmware macros and never appears as extrusion
(measured 2026-08-28: slicer 46.35 g, G-code E-moves about 6.8 g), and the parser
counts un-retracts as extrusion. Same print, same stop, two different weights.

One fixture, both writers. Expected values are worked out by hand:

* slot 1: estimate 30.00 g, 900 of its 1500 mm extruded by layer 50 -> 0.6 -> 18.00 g
* slot 2: estimate 16.35 g, 300 of its  750 mm extruded by layer 50 -> 0.4 ->  6.54 g

The G-code's own grams at layer 50 would be 2.68 g and 0.89 g (PLA, 1.75 mm).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import spoolman_charge
from backend.app.services.usage_tracker import _active_sessions
from backend.app.utils.threemf_tools import partial_slot_grams
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import (  # noqa: F401 — fixtures
    _clean_memory,
    history,
    next_print_state,
    run_internal,
    session,
    stage,
)
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = pytest.mark.usefixtures("spoolman_charge_admitted")

#: The slicer's per-slot estimates — flush and purge included (46.35 g in all).
TWO_COLOURS = [
    {"slot_id": 1, "used_g": 30.0, "type": "PLA", "color": "#FF0000"},
    {"slot_id": 2, "used_g": 16.35, "type": "PLA", "color": "#00FF00"},
]
#: Cumulative E millimetres per filament at each layer change, as the parser returns them.
TIMELINE = {25: {0: 400.0, 1: 100.0}, 50: {0: 900.0, 1: 300.0}, 100: {0: 1500.0, 1: 750.0}}
TIMELINE_JSON = {str(layer): {str(fid): mm for fid, mm in slots.items()} for layer, slots in TIMELINE.items()}

EXPECTED_AT_50 = {1: 18.0, 2: 6.54}


def stopped(layer=50, *, total=100, progress=41.0):
    return {
        "status": "failed",
        "last_layer_num": layer,
        "last_progress": progress,
        **terminal_snapshot_fields(layer_num=layer, total_layers=total, progress=progress),
    }


COMPLETED = {
    "status": "completed",
    "last_layer_num": 100,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=100, total_layers=100, progress=100.0),
}

REELS = {"TRAY0": {"id": 11, "filament": {"density": 1.24}}, "TRAY1": {"id": 12, "filament": {"density": 1.24}}}


def _tracking(usage, layer_usage):
    return SimpleNamespace(
        archive_id=144,
        filament_usage=usage,
        layer_usage=layer_usage,
        filament_properties={},
        ams_trays={0: {"tray_uuid": "TRAY0", "tray_type": "PLA"}, 1: {"tray_uuid": "TRAY1", "tray_type": "PLA"}},
        slot_to_tray=[0, 1],
        tray_remain_start={},
    )


def _charged(client):
    charged: dict[int, float] = {}
    for call in client.use_spool.await_args_list:
        charged[call.args[0]] = round(charged.get(call.args[0], 0.0) + call.args[1], 2)
    return charged


def _client():
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: REELS.get(tag))
    client.use_spool = AsyncMock()
    return client


async def spoolman_stopped(data, *, usage=TWO_COLOURS, layer_usage=TIMELINE_JSON):
    """The partial Spoolman writer's real entry point, Spoolman and the database faked."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    client = _client()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=client)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", next_print_state([])),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", AsyncMock()),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=_tracking(usage, layer_usage),
            last_layer_num=data["last_layer_num"],
            last_progress=data["last_progress"],
            completion=CompletionSnapshot.from_event(data),
        )
    return _charged(client)


async def spoolman_completed(data, *, usage=TWO_COLOURS, layer_usage=TIMELINE_JSON):
    from backend.app.services.spoolman_tracking import report_usage

    client = _client()
    with (
        patch(
            "backend.app.services.spoolman_tracking.async_session",
            lambda: _AsyncCtx(_make_db(_tracking(usage, layer_usage))),
        ),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=client)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", next_print_state([])),
    ):
        await report_usage(printer_id=1, archive_id=144, completion=CompletionSnapshot.from_event(data))
    return _charged(client)


async def internal(db_session, stage, data):  # noqa: F811 — the imported fixture's value
    """The internal writer on the same fixture; returns {slot: grams} by the slot's reel."""
    _active_sessions[stage.printer.id] = session(
        stage.printer.id, spool_assignments={(0, 0): stage.a, (0, 1): stage.b}, started_at=None, ams_mapping=[0, 1]
    )
    await run_internal(
        db_session, stage.printer, stage.archive, data, next_print_state([]), usage=TWO_COLOURS, layer_usage=TIMELINE
    )
    by_reel = dict(await history(db_session))
    return {1: by_reel.get(stage.a), 2: by_reel.get(stage.b)}


class TestTheSharedMeasure:
    def test_estimate_times_the_slots_own_progress(self):
        assert partial_slot_grams(TWO_COLOURS, TIMELINE, 50) == pytest.approx(EXPECTED_AT_50)

    def test_a_slot_the_timeline_does_not_know_is_left_to_the_caller(self):
        usage = [*TWO_COLOURS, {"slot_id": 3, "used_g": 5.0}]
        assert set(partial_slot_grams(usage, TIMELINE, 50)) == {1, 2}

    def test_a_slot_with_no_estimate_has_no_weight(self):
        assert partial_slot_grams([{"slot_id": 1, "used_g": 0}], TIMELINE, 50) == {}

    def test_never_more_than_the_estimate(self):
        assert partial_slot_grams(TWO_COLOURS, TIMELINE, 400) == pytest.approx({1: 30.0, 2: 16.35})


class TestAStoppedPrintWeighsTheSameInBothBackends:
    async def test_the_internal_writer_is_the_reference(self, db_session, stage):  # noqa: F811
        # The internal ledger keeps one decimal: 18.0 and 6.5.
        assert await internal(db_session, stage, stopped()) == pytest.approx(EXPECTED_AT_50, abs=0.05)

    async def test_spoolman_charges_the_estimate_times_the_gcode_progress(self):
        """Was {11: 2.68, 12: 0.89}: the plate G-code's own grams, blind to every flush."""
        assert await spoolman_stopped(stopped()) == pytest.approx({11: 18.0, 12: 6.54})

    async def test_unretracts_no_longer_inflate_the_charge(self):
        """A retract-heavy single-colour print: the parser's E total reads 42 g for a 30 g
        estimate. Stopped half way along it, the print used 15 g — not the 21 g of E-moves."""
        usage = [{"slot_id": 1, "used_g": 30.0}]
        # 42 g of PLA at 1.75 mm is 14 082 mm; half of it by layer 50.
        timeline = {"50": {"0": 7041.0}, "100": {"0": 14082.0}}
        assert await spoolman_stopped(stopped(), usage=usage, layer_usage=timeline) == pytest.approx({11: 15.0})

    async def test_a_stop_before_the_first_recorded_layer_charges_nothing(self):
        assert await spoolman_stopped(stopped(layer=10)) == {}

    async def test_without_an_observed_layer_the_progress_sizes_the_weight(self):
        """No observed layer: the percentage stands in, as in the internal writer — the timeline
        is never read at a layer estimated from it (30 x 0.41, 16.35 x 0.41)."""
        data = stopped(layer=0, progress=41.0)
        data["last_layer_num"] = 0
        assert await spoolman_stopped(data) == pytest.approx({11: 12.3, 12: 6.7}, abs=0.006)


class TestACompletedPrintIsChargedItsEstimate:
    """GUARD: nothing above touches a print that finished — both backends charge the slicer's
    estimate, whatever the G-code timeline says."""

    async def test_internal(self, db_session, stage):  # noqa: F811
        assert await internal(db_session, stage, COMPLETED) == pytest.approx({1: 30.0, 2: 16.35}, abs=0.05)

    async def test_spoolman(self):
        assert await spoolman_completed(COMPLETED) == {11: 30.0, 12: 16.35}
