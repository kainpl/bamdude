"""Usage lost because Spoolman did not answer is said out loud (upstream fb69a665d, leg 3).

The tracking row is gone by the time the client is asked, and there is no retry: a print
that ended while Spoolman was down is never charged. The only trace was a WARNING naming
neither the printer nor the print. Now the log names the printer, the archive, each tray
(with the Spoolman spool bound to it, when a local slot assignment knows it) and its grams,
and the existing "usage not recorded" notice is raised with the same figures.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import next_print_state
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

TWO_COLOURS = [{"slot_id": 1, "used_g": 30.0}, {"slot_id": 2, "used_g": 16.35}]
TIMELINE = {"50": {"0": 900.0, "1": 300.0}, "100": {"0": 1500.0, "1": 750.0}}

COMPLETED = {
    "status": "completed",
    "last_layer_num": 100,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=100, total_layers=100, progress=100.0),
}
STOPPED_AT_50 = {
    "status": "failed",
    "last_layer_num": 50,
    "last_progress": 41.0,
    **terminal_snapshot_fields(layer_num=50, total_layers=100, progress=41.0),
}


def _tracking(**overrides):
    fields = {
        "archive_id": 144,
        "filament_usage": TWO_COLOURS,
        "layer_usage": TIMELINE,
        "filament_properties": {},
        "ams_trays": {0: {"tray_uuid": "TRAY0", "tray_type": "PLA"}, 1: {"tray_uuid": "TRAY1", "tray_type": "PLA"}},
        "slot_to_tray": [0, 1],
        "tray_remain_start": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def _bound_spool(_printer_id, ams_id, tray_id):
    """Tray 0 is bound to Spoolman spool 17 by a local slot assignment; tray 1 only by its tag."""
    return 17 if (ams_id, tray_id) == (0, 0) else None


def _patches(tracking, notify, *, db):
    return (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=None)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment", _bound_spool),
        patch("backend.app.services.printer_manager.printer_manager", next_print_state([])),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    )


async def completed_while_down(tracking, caplog):
    from backend.app.services.spoolman_tracking import report_usage

    notify = AsyncMock()
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman_tracking"):
        p = _patches(tracking, notify, db=_make_db(tracking))
        with p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]:
            await report_usage(printer_id=3, archive_id=144, completion=CompletionSnapshot.from_event(COMPLETED))
    return notify


async def stopped_while_down(tracking, caplog):
    from backend.app.services.spoolman_tracking import _report_partial_usage

    notify = AsyncMock()
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman_tracking"):
        p = _patches(tracking, notify, db=AsyncMock())
        with p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]:
            await _report_partial_usage(
                printer_id=3,
                tracking=tracking,
                last_layer_num=50,
                last_progress=41.0,
                completion=CompletionSnapshot.from_event(STOPPED_AT_50),
            )
    return notify


def _lost(notify):
    assert notify.await_count == 1
    printer_id, uncharged = notify.await_args.args[0], notify.await_args.args[1]
    return printer_id, sorted(uncharged)


class TestACompletedPrint:
    @pytest.mark.asyncio
    async def test_the_notice_carries_every_tray_and_its_grams(self, caplog):
        notify = await completed_while_down(_tracking(), caplog)
        assert _lost(notify) == (3, [(0, 30.0), (1, 16.35)])

    @pytest.mark.asyncio
    async def test_the_log_names_the_print_the_spool_and_the_grams(self, caplog):
        await completed_while_down(_tracking(), caplog)
        text = caplog.text
        assert "printer=3" in text and "archive=144" in text
        assert "tray 0 (spool 17): 30.00 g" in text
        assert "tray 1: 16.35 g" in text
        assert "not retried" in text

    @pytest.mark.asyncio
    async def test_a_print_with_no_estimate_is_still_named(self, caplog):
        """No 3MF: the remain-delta needs the spool's weight from Spoolman, so no grams are
        known — the print is named all the same, and no notice invents a figure."""
        tracking = _tracking(filament_usage=[], layer_usage=None, tray_remain_start={"0-0": {"remain": 80}})
        notify = await completed_while_down(tracking, caplog)
        notify.assert_not_awaited()
        assert "printer=3" in caplog.text and "archive=144" in caplog.text


class TestAStoppedPrint:
    @pytest.mark.asyncio
    async def test_the_notice_carries_what_the_print_used(self, caplog):
        """30 g x 900/1500 and 16.35 g x 300/750 — what would have been charged."""
        notify = await stopped_while_down(_tracking(), caplog)
        assert _lost(notify) == (3, [(0, 18.0), (1, 6.54)])

    @pytest.mark.asyncio
    async def test_the_log_names_the_print_the_spool_and_the_grams(self, caplog):
        await stopped_while_down(_tracking(), caplog)
        text = caplog.text
        assert "printer=3" in text and "archive=144" in text
        assert "tray 0 (spool 17): 18.00 g" in text
        assert "tray 1: 6.54 g" in text
