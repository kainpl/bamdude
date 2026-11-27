"""An aborted Spoolman print with no stored mapping is charged to the tray the printer named
(upstream 991d2caca, leg 2).

A print started from the slicer on a printer whose request topic we cannot read (X1 family,
cloud prints) has no captured mapping. At completion the writer asks the printer's own tray
reading (``_completion_slot_to_tray``); the aborted path did not, and charged by slot
POSITION — a one-colour print fed from AMS tray 3 and cancelled at 40 % charged tray 1's
spool, while the same print left to finish charged tray 3's.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import spoolman_charge
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = pytest.mark.usefixtures("spoolman_charge_admitted")

#: Four loaded AMS trays; Spoolman knows each by its tag.
AMS_TRAYS = {tray: {"tray_uuid": f"TRAY{tray}", "tray_type": "PLA"} for tray in range(4)}
REELS = {f"TRAY{tray}": {"id": 10 + tray, "filament": {}} for tray in range(4)}
ONE_COLOUR = [{"slot_id": 1, "used_g": 50.0}]

CANCELLED_AT_40_PERCENT = {
    "status": "aborted",
    "last_layer_num": 80,
    "last_progress": 40.0,
    **terminal_snapshot_fields(layer_num=80, total_layers=200, progress=40.0),
}


def printer(*, tray_now=255, last_loaded_tray=-1, tray_change_log=(), vt_tray=None):
    state = MagicMock()
    state.raw_data = {"ams": [], "vt_tray": vt_tray or []}
    state.tray_now = tray_now
    state.last_loaded_tray = last_loaded_tray
    state.tray_change_log = list(tray_change_log)
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


async def cancelled(pm, *, usage=ONE_COLOUR, slot_to_tray=None, ams_trays=AMS_TRAYS, reels=REELS):
    """The partial Spoolman writer's real entry point; returns ({spool: grams}, the notice mock)."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=usage,
        layer_usage=None,
        filament_properties={},
        ams_trays=ams_trays,
        slot_to_tray=slot_to_tray,
        tray_remain_start={},
    )
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: reels.get(tag))
    client.use_spool = AsyncMock()
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=client)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch(
            "backend.app.services.spoolman_tracking._resolve_bound_spool",
            AsyncMock(return_value=None),
        ),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", pm),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            last_layer_num=CANCELLED_AT_40_PERCENT["last_layer_num"],
            last_progress=CANCELLED_AT_40_PERCENT["last_progress"],
            completion=CompletionSnapshot.from_event(CANCELLED_AT_40_PERCENT),
        )
    charged = {call.args[0]: call.args[1] for call in client.use_spool.await_args_list}
    return charged, notify


class TestAnAbortedPrintWithoutAMapping:
    @pytest.mark.asyncio
    async def test_the_tray_the_printer_is_feeding_from_is_charged(self):
        """50 g estimate, stopped at layer 80 of 200: 20 g — to tray 3's spool (12), not tray 1's (10)."""
        charged, _ = await cancelled(printer(tray_now=2))
        assert charged == {12: 20.0}

    @pytest.mark.asyncio
    async def test_the_last_loaded_tray_answers_once_the_printer_has_retracted(self):
        charged, _ = await cancelled(printer(tray_now=255, last_loaded_tray=3))
        assert charged == {13: 20.0}

    @pytest.mark.asyncio
    async def test_a_single_logged_tray_answers(self):
        charged, _ = await cancelled(printer(tray_now=255, tray_change_log=[(1, 0)]))
        assert charged == {11: 20.0}

    @pytest.mark.asyncio
    async def test_the_right_hand_external_holder_answers_on_a_two_holder_printer(self):
        trays = {**AMS_TRAYS, 254: {"tray_uuid": "LEFT", "tray_type": "PLA"}, 255: {"tray_uuid": "RIGHT"}}
        reels = {**REELS, "LEFT": {"id": 54, "filament": {}}, "RIGHT": {"id": 55, "filament": {}}}
        pm = printer(tray_now=255, vt_tray=[{"id": 254, "tray_type": "PLA"}, {"id": 255, "tray_type": "PLA"}])
        charged, _ = await cancelled(pm, ams_trays=trays, reels=reels)
        assert charged == {55: 20.0}

    @pytest.mark.asyncio
    async def test_with_nothing_named_the_position_is_still_charged_and_said_to_be_a_guess(self, caplog):
        """No mapping and no tray named by the printer: the positional default stands, as at
        completion — charged, and logged at WARNING as a guess."""
        import logging

        with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman_tracking"):
            charged, _ = await cancelled(printer(tray_now=255))
        assert charged == {10: 20.0}
        assert "charging by tray position" in caplog.text

    @pytest.mark.asyncio
    async def test_a_multi_colour_print_without_a_mapping_stays_positional(self):
        usage = [{"slot_id": 1, "used_g": 50.0}, {"slot_id": 2, "used_g": 10.0}]
        charged, _ = await cancelled(printer(tray_now=2), usage=usage)
        assert charged == {10: 20.0, 11: 4.0}


class TestADispatchedMappingStillWins:
    @pytest.mark.asyncio
    async def test_the_stored_mapping_outranks_the_printers_tray(self):
        """GUARD: the mapping the print was dispatched with is the first authority — the
        printer's current tray (2) does not replace it."""
        charged, _ = await cancelled(printer(tray_now=2), slot_to_tray=[1])
        assert charged == {11: 20.0}
