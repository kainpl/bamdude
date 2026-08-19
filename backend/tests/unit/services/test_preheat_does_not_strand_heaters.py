"""Preheat must not leave a machine heating for a print that is not happening.

Three faults, all ours, taken from the supporting half of upstream #2727.

⚠️ Stopping a queue item only writes ``status`` to the database, which a
dispatch coroutine parked in ``asyncio.sleep`` cannot observe. During the
preheat stage there is no print to stop either — the stop command goes to an
idle printer — so the heaters ran for the rest of the wait plus the soak while
the printer stayed claimed, blocking everything queued behind a print that was
not happening.

⚠️ A dispatch that died after preheat — a failed upload, an exception — left the
heaters on because nothing knew they had been turned on.

⚠️ And a chamber-heated print whose file names no bed temperature skipped the
stage entirely and started cold. Orca exports routinely name none.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services import preheat as preheat_service


@pytest.fixture(autouse=True)
def _clean():
    preheat_service._pinned.clear()
    yield
    preheat_service._pinned.clear()


@pytest.fixture
def client():
    return MagicMock()


class TestUnwindingWhatPreheatSent:
    def test_each_command_is_undone(self, client):
        preheat_service._pinned[1] = {"bed", "chamber", "airduct"}

        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = client
            preheat_service.rollback(1)

        client.set_bed_temperature.assert_called_once_with(0)
        client.set_chamber_temperature.assert_called_once_with(0)
        client.set_airduct_mode.assert_called_once_with("cooling")

    def test_only_what_was_actually_sent(self, client):
        """A printer with no chamber heater never got a chamber command, and
        sending it one on the way out would be inventing traffic."""
        preheat_service._pinned[1] = {"bed"}

        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = client
            preheat_service.rollback(1)

        client.set_bed_temperature.assert_called_once_with(0)
        client.set_chamber_temperature.assert_not_called()

    def test_nothing_pinned_is_a_no_op(self, client):
        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = client
            preheat_service.rollback(99)

        client.set_bed_temperature.assert_not_called()

    def test_a_started_print_owns_the_heaters(self, client):
        """⚠️ Once the print is running, undoing preheat would switch the bed
        off underneath it."""
        preheat_service._pinned[1] = {"bed", "chamber"}

        preheat_service.clear_pin(1)
        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = client
            preheat_service.rollback(1)

        client.set_bed_temperature.assert_not_called()

    def test_it_never_raises_on_the_failure_path(self, client):
        """It runs inside an ``except``/``finally``; raising here would mask
        the real exception."""
        preheat_service._pinned[1] = {"bed", "chamber"}
        client.set_bed_temperature.side_effect = RuntimeError("printer went away")

        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = client
            preheat_service.rollback(1)  # must not raise

        client.set_chamber_temperature.assert_called_once_with(0)

    def test_a_missing_client_does_not_raise_either(self):
        preheat_service._pinned[1] = {"bed"}

        with patch("backend.app.services.preheat.printer_manager") as pm:
            pm.get_client.return_value = None
            preheat_service.rollback(1)

        assert 1 not in preheat_service._pinned

    def test_a_cooling_airduct_flip_is_not_recorded(self):
        """⚠️ Putting the flap back to cooling IS the rollback. Recording a
        cooling set would make the undo a no-op that looks done."""
        import inspect

        source = inspect.getsource(preheat_service.preheat_and_soak)
        assert 'if desired_airduct == "heating":' in source


class TestAStoppedItemReachesTheDispatch:
    def test_a_live_job_for_that_item_is_signalled(self):
        from backend.app.services.background_dispatch import background_dispatch

        job = SimpleNamespace(id=7, queue_item_id=42)
        background_dispatch._active_jobs[7] = SimpleNamespace(job=job)
        try:
            assert background_dispatch.cancel_dispatch_for_queue_item(42) is True
            assert background_dispatch._is_cancel_requested(7) is True
        finally:
            background_dispatch._active_jobs.pop(7, None)
            background_dispatch._cancel_requested_job_ids.discard(7)

    def test_another_items_job_is_left_alone(self):
        from backend.app.services.background_dispatch import background_dispatch

        job = SimpleNamespace(id=8, queue_item_id=42)
        background_dispatch._active_jobs[8] = SimpleNamespace(job=job)
        try:
            assert background_dispatch.cancel_dispatch_for_queue_item(43) is False
            assert background_dispatch._is_cancel_requested(8) is False
        finally:
            background_dispatch._active_jobs.pop(8, None)

    def test_the_stop_route_signals_before_it_sends_the_stop(self):
        """⚠️ Order matters: during preheat there is no print for the stop
        command to act on, so the signal is the only thing that ends it."""
        import inspect

        from backend.app.api.routes import print_queue

        source = inspect.getsource(print_queue)
        block = source[source.index("Can only stop items that are printing") :]
        signal = block.index("cancel_dispatch_for_queue_item")
        stop = block.index("printer_manager.stop_print")
        assert signal < stop


class TestAChamberPrintWithNoBedTemperature:
    def test_it_no_longer_skips_the_whole_stage(self):
        """⚠️ Orca-exported 3MFs routinely carry no bed temperature, and
        skipping meant those prints started with a cold chamber — the exact
        thing the stage exists to prevent."""
        import inspect

        source = inspect.getsource(preheat_service.preheat_and_soak)
        block = source[source.index("bed_target = int(archive.bed_temperature)") :]
        block = block[: block.index("client = printer_manager.get_client")]
        assert "if chamber_target <= 0:" in block, "a print with no chamber requirement must still skip"
        assert "_CHAMBER_HEATING_BED_FLOOR" in block

    def test_the_floor_is_a_chamber_setting_not_a_print_surface(self):
        """It drives the chamber through the bed; the print issues its own
        M140/M190 at start, so this never reaches the part."""
        assert preheat_service._CHAMBER_HEATING_BED_FLOOR == 90
