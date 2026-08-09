"""Physical commands are refused while a job is on the printer.

BambuStudio can answer this in the UI — it is one desktop window, so a greyed-out
button is a sufficient guard. Ours is an HTTP surface reachable by API key, by
the Telegram bot, and by a browser tab that was opened before the print started,
so the answer has to live on the server. Five entry points had no server-side
check at all: bed jog, auto-home, device calibration (route **and** bot), and
single-printer firmware preparation — while the *bulk* firmware path did check,
which is the tell that the rule was known and unevenly applied.

The rule itself is BS's ``is_in_printing_status``: PAUSE, RUNNING, SLICING,
PREPARE. Note the last two — the old copy of this rule in ``firmware_batch``
asked only for RUNNING/PAUSE and let PREPARE through, which is a printer already
heating and positioning for a job.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.printer_manager import BUSY_PRINT_STATES, is_printer_busy


def _client(state_str: str) -> MagicMock:
    c = MagicMock()
    c.state = MagicMock()
    c.state.state = state_str
    c.state.connected = True
    return c


class TestTheRuleIsBambuStudios:
    def test_the_four_busy_states_are_bs_is_in_printing_status(self) -> None:
        assert {"RUNNING", "PAUSE", "SLICING", "PREPARE"} == BUSY_PRINT_STATES

    @pytest.mark.parametrize("state_str", sorted({"RUNNING", "PAUSE", "SLICING", "PREPARE"}))
    def test_busy_states_are_busy(self, state_str: str) -> None:
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = _client(state_str)
            assert is_printer_busy(1) is True

    @pytest.mark.parametrize("state_str", ["IDLE", "FINISH", "FAILED", "OFFLINE", ""])
    def test_everything_else_is_free(self, state_str: str) -> None:
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = _client(state_str)
            assert is_printer_busy(1) is False

    def test_prepare_is_the_state_the_old_narrower_rule_let_through(self) -> None:
        """Its own test because it is the behaviour change, not a restatement:
        ``firmware_batch._is_printing`` used to be ``in ("RUNNING", "PAUSE")``."""
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = _client("PREPARE")
            assert is_printer_busy(1) is True

    def test_an_unknown_printer_is_not_reported_busy(self) -> None:
        """ "Disconnected" is answered by the connection check every caller does
        first. Answering True here would turn a dropped link into a permanent
        refusal that no amount of waiting clears."""
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = None
            assert is_printer_busy(1) is False


@pytest.mark.asyncio
class TestTheRoutesRefuse:
    """409, not 400: the request is well-formed, the machine's state is the
    problem, and it stops being the problem on its own."""

    async def _post(self, async_client, url: str) -> int:
        with (
            patch("backend.app.api.routes.printers.printer_manager") as pm,
            patch("backend.app.api.routes.printers.is_printer_busy", return_value=True),
        ):
            pm.get_client.return_value = _client("RUNNING")
            pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
            r = await async_client.post(url)
        return r.status_code

    async def test_bed_jog_refuses(self, async_client, printer_factory) -> None:
        printer = await printer_factory(model="X1C")
        assert await self._post(async_client, f"/api/v1/printers/{printer.id}/bed-jog?distance=1") == 409

    async def test_home_axes_refuses(self, async_client, printer_factory) -> None:
        printer = await printer_factory(model="X1C")
        assert await self._post(async_client, f"/api/v1/printers/{printer.id}/home-axes?axes=all") == 409

    async def test_calibration_refuses(self, async_client, printer_factory) -> None:
        printer = await printer_factory(model="X1C")
        url = f"/api/v1/printers/{printer.id}/calibration?bed_leveling=true"
        assert await self._post(async_client, url) == 409

    async def test_an_idle_printer_still_gets_through_the_guard(self, async_client, printer_factory) -> None:
        """Without this the suite would pass with the guard hardcoded to True."""
        printer = await printer_factory(model="X1C")
        with (
            patch("backend.app.api.routes.printers.printer_manager") as pm,
            patch("backend.app.api.routes.printers.is_printer_busy", return_value=False),
        ):
            client = _client("IDLE")
            client.send_gcode.return_value = True
            pm.get_client.return_value = client
            pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
            r = await async_client.post(f"/api/v1/printers/{printer.id}/home-axes?axes=all")

        assert r.status_code == 200
        client.send_gcode.assert_called_once_with("G28")


@pytest.mark.asyncio
class TestFirmwarePreparationRefuses:
    async def test_prepare_update_reports_busy_and_stops(self, db_session, printer_factory) -> None:
        """The bulk path already skipped a printing printer; preparing one
        single-handed did not, so the safe route was the batch one."""
        from backend.app.services.firmware_update import FirmwareUpdateService

        printer = await printer_factory(model="X1C")
        with (
            patch("backend.app.services.firmware_update.printer_manager") as pm,
            patch("backend.app.services.firmware_update.is_printer_busy", return_value=True),
        ):
            pm.get_client.return_value = _client("RUNNING")
            result = await FirmwareUpdateService().prepare_update(printer.id, db_session)

        assert result["can_proceed"] is False
        assert any("busy" in e.lower() for e in result["errors"])


class TestTheBulkPathUsesTheSameRule:
    def test_is_printing_delegates(self) -> None:
        """Two copies of a safety rule is how one of them gets fixed alone."""
        from backend.app.services import firmware_batch

        with patch("backend.app.services.firmware_batch.is_printer_busy", return_value=True) as m:
            assert firmware_batch._is_printing(7) is True
        m.assert_called_once_with(7)
