"""Drying must not be torn down for a print that cannot start.

Ported from upstream #2801. A printer sitting in FINISH with an unacknowledged
plate and something pending in its queue stopped and restarted drying **once
per scheduler tick** — the reporter's Home Assistant history recorded about
2000 state changes over ten days, no cycle ever ran long enough to remove
moisture, and cycles started by hand on the printer's other AMS units were torn
down with it.

Two questions had become tangled:

- **plate-clear** answers "is the bed ready for the next job" and says nothing
  about whether the AMS may heat. The gap between a finished print and the
  acknowledgment is exactly when drying is most useful — the printer is free and
  nobody is waiting on it — and leaving a plate unacknowledged is also how
  people hold the queue by hand.
- **the dispatch set** means "the queue could not dispatch here this pass", not
  "this printer is printing". Reading it as the latter put a plate-held printer
  down the mid-print path: its drying temperature was capped by the mid-print
  spool protection and the cycle was logged as (mid-print) while it stood idle.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ⚠️ Imported for its side effect, not its name: Printer declares a
# relationship to PrinterLocation by string, and SQLAlchemy cannot resolve it
# unless that module has been imported. Without this the file passes only when
# some other test in the run happens to have imported it first.
import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import PrintScheduler


def _ams(humidity: str = "80", dry_time: int = 0) -> dict:
    return {
        "id": 0,
        "module_type": "n3f",
        "dry_time": dry_time,
        "humidity_raw": humidity,
        "dry_sf_reason": [],
        "tray": [{"tray_type": "PLA"}],
    }


def _state(printer_state: str, *, dry_time: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        raw_data={"ams": [_ams(dry_time=dry_time)]},
        firmware_version="01.03.00.00",
        state=printer_state,
    )


def _settings(**overrides: str) -> dict:
    values = {
        "queue_drying_enabled": "true",
        "ambient_drying_enabled": "false",
        "print_drying_enabled": "false",
        "ams_humidity_fair": "60",
        "queue_drying_block": "false",
    }
    values.update(overrides)
    return values


def _db(values: dict, *, printer_ids: tuple[int, ...] = (1,)) -> AsyncMock:
    """Answer the two queries the drying pass makes: settings, then printers.

    ⚠️ Settings are matched on the BIND PARAMETER, not on ``str(statement)`` —
    SQLAlchemy renders the key as a placeholder, so every settings query reads
    identically and a substring match would return the first value for all of
    them.
    """
    db = AsyncMock()

    async def execute(statement, *args, **kwargs):
        result = MagicMock()
        try:
            params = list(statement.compile(compile_kwargs={"literal_binds": False}).params.values())
        except Exception:
            params = []
        for key, value in values.items():
            if key in params:
                result.scalar_one_or_none.return_value = SimpleNamespace(value=value)
                return result
        if "printer" in str(statement).lower():
            printers = []
            for printer_id in printer_ids:
                printer = MagicMock()
                printer.id = printer_id
                printer.is_active = True
                printers.append(printer)
            scalars = MagicMock()
            scalars.__iter__ = MagicMock(return_value=iter(printers))
            result.scalars.return_value = scalars
            return result
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.fixture
def scheduler():
    return PrintScheduler()


class TestAPlateHoldDoesNotStopDrying:
    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_finished_printer_with_an_uncleared_plate_still_dries(self, mock_pm, scheduler):
        """⚠️ The whole point: FINISH with the plate unacknowledged is not a
        reason to refuse the AMS heat."""
        mock_pm.get_status.return_value = _state("FINISH")
        mock_pm.is_connected.return_value = True
        mock_pm.get_model.return_value = "H2D"
        mock_pm.is_awaiting_plate_clear.return_value = True
        mock_pm.send_drying_command.return_value = True

        # Idle in every sense EXCEPT the plate, which is what the real predicate
        # would report for this printer.
        scheduler._is_printer_idle = MagicMock(
            side_effect=lambda pid, require_plate_clear=True: not require_plate_clear
        )

        await scheduler._check_auto_drying(_db(_settings(ambient_drying_enabled="true")), [], set())

        mock_pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_it_dries_at_the_full_preset(self, mock_pm, scheduler):
        """A plate-held printer is IDLE, so it dries at the full preset — not at
        the reduced temperature the mid-print spool protection imposes."""
        mock_pm.get_status.return_value = _state("FINISH")
        mock_pm.is_connected.return_value = True
        mock_pm.get_model.return_value = "H2D"
        mock_pm.is_awaiting_plate_clear.return_value = True
        mock_pm.send_drying_command.return_value = True
        scheduler._is_printer_idle = MagicMock(
            side_effect=lambda pid, require_plate_clear=True: not require_plate_clear
        )

        # print-drying enabled and hardware that can dry through one: under the
        # old inference this was all it took to reach the mid-print path.
        await scheduler._check_auto_drying(
            _db(_settings(ambient_drying_enabled="true", print_drying_enabled="true")), [], set()
        )

        temp = mock_pm.send_drying_command.call_args.args[2]
        assert temp == 45, f"the full PLA preset, not the mid-print cap: got {temp}"

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_printer_the_queue_could_not_reach_is_not_mid_print(self, mock_pm, scheduler):
        """⚠️ The inference this port removes.

        Everything the queue loop could not dispatch to used to land in the same
        set, and drying read that set as "is printing". With print-drying on and
        hardware that can dry through a print, a printer standing in FINISH was
        therefore dried at the mid-print cap and logged as (mid-print). It is not
        printing, so the correct answer is to leave it alone."""
        mock_pm.get_status.return_value = _state("FINISH")
        mock_pm.is_connected.return_value = True
        mock_pm.get_model.return_value = "H2D"
        mock_pm.send_drying_command.return_value = True
        scheduler._is_printer_idle = MagicMock(return_value=True)

        await scheduler._check_auto_drying(
            _db(_settings(ambient_drying_enabled="true", print_drying_enabled="true")), [], {1}
        )

        mock_pm.send_drying_command.assert_not_called()


class TestAPrintingPrinterIsStillLeftAlone:
    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_running_printer_is_skipped_when_it_cannot_dry_through(self, mock_pm, scheduler):
        mock_pm.get_status.return_value = _state("RUNNING")
        mock_pm.is_connected.return_value = True
        mock_pm.get_model.return_value = "P1S"  # not on the dry-while-printing allowlist
        mock_pm.send_drying_command.return_value = True
        scheduler._is_printer_idle = MagicMock(return_value=True)

        await scheduler._check_auto_drying(_db(_settings(ambient_drying_enabled="true")), [], set())

        mock_pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_printer_about_to_print_is_skipped(self, mock_pm, scheduler):
        """Held post-dispatch — the narrow set still means "leave it alone"."""
        mock_pm.get_status.return_value = _state("IDLE")
        mock_pm.is_connected.return_value = True
        mock_pm.get_model.return_value = "H2D"
        mock_pm.send_drying_command.return_value = True
        scheduler._is_printer_idle = MagicMock(return_value=True)

        await scheduler._check_auto_drying(_db(_settings(ambient_drying_enabled="true")), [], {1})

        mock_pm.send_drying_command.assert_not_called()


class TestOnlyOurOwnCyclesAreStopped:
    """⚠️ A print took priority over EVERY drying AMS on the printer.

    One auto-dried unit was enough to send a stop to a cycle the operator had
    started by hand on a different unit of the same machine. The entry gate only
    ever knew about cycles we began, so the action must not reach past them.
    """

    @staticmethod
    def _two_units(scheduler, mock_pm, *, ours: int | None) -> None:
        state = SimpleNamespace(
            raw_data={"ams": [_ams(dry_time=90) | {"id": 0}, _ams(dry_time=90) | {"id": 1}]},
            firmware_version="01.03.00.00",
            state="FINISH",
        )
        mock_pm.get_status.return_value = state
        scheduler._drying_in_progress = {1: 0.0}
        if ours is not None:
            scheduler._auto_dried_units = {(1, ours): 0.0}

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_manual_cycle_on_another_unit_is_left_running(self, mock_pm, scheduler):
        self._two_units(scheduler, mock_pm, ours=0)

        await scheduler._stop_drying(1)

        stopped = [call.args[1] for call in mock_pm.send_drying_command.call_args_list]
        assert stopped == [0], f"only the unit we armed should be stopped, got AMS {stopped}"

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_nothing_is_stopped_when_we_armed_nothing(self, mock_pm, scheduler):
        """After a restart we cannot prove a running cycle is ours, so we leave
        it alone rather than risk stopping somebody's manual dry."""
        self._two_units(scheduler, mock_pm, ours=None)

        await scheduler._stop_drying(1)

        mock_pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_finished_cycle_stops_being_ours(self, mock_pm, scheduler):
        """Otherwise the claim outlives its cycle and authorises stopping a
        manual dry started later on the same unit."""
        import time as _time

        scheduler._auto_dried_units = {(1, 0): _time.monotonic() - 3600}
        mock_pm.get_status.return_value = _state("IDLE")  # AMS 0 reports dry_time 0

        scheduler._sync_drying_state()

        assert scheduler._auto_dried_units == {}

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_a_just_armed_claim_survives_the_sweep(self, mock_pm, scheduler):
        """⚠️ ``dry_time`` does not appear in the printer's report the instant
        the command lands, and a claim is never re-adopted — so a claim dropped
        in that window would be lost for the whole cycle."""
        import time as _time

        scheduler._auto_dried_units = {(1, 0): _time.monotonic()}
        mock_pm.get_status.return_value = _state("IDLE")

        scheduler._sync_drying_state()

        assert (1, 0) in scheduler._auto_dried_units


class TestQueueDryingBlockFinallyMeansSomething:
    """The setting sat inside the not-idle branch, where both of its answers led
    to the same skip — so all it ever decided was whether a cycle got needlessly
    killed on the way past. On the dispatch path it does what it says: a queued
    print waits for a running cycle.

    ⚠️ Asserted on the ORDER of the checks rather than behaviourally. What went
    wrong was placement, and a behavioural test that drove the whole of
    ``check_queue`` would pass with the check back in the branch it came from —
    it skipped there too.
    """

    def test_the_hold_is_asked_after_the_idle_gate(self):
        import inspect

        source = inspect.getsource(PrintScheduler.check_queue)
        idle_gate = source.index("if not printer_idle:")
        hold = source.index('"queue_drying_block"')
        dispatch = source.index("await self._start_print(db, item)")

        assert idle_gate < hold < dispatch, (
            "the drying hold belongs with the availability checks, after the idle "
            "gate and before dispatch — inside the not-idle branch it decides nothing"
        )
