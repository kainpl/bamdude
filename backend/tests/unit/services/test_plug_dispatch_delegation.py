"""Both energy paths must resolve the driver, not re-implement the routing.

Phase 0 of the Zigbee work found the API and the automation disagreeing about
which driver a plug used, because the resolver existed in two verbatim copies.
These two sites are the same defect one layer down: they branch on ``plug_type``
by hand, so a plug type they predate is either silently misrouted (``main.py``
ends its chain with ``else: tasmota_service``, an HTTP poll against an IP a
Zigbee plug does not have) or silently skipped (``archives.py`` has no ``else``
and simply adds nothing).

Both feed per-print energy, which is why this is the same failure mode phase 0
had to fix: the accounting comes out wrong rather than absent.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _plug(plug_type: str, plug_id: int = 1):
    return SimpleNamespace(id=plug_id, name=f"plug-{plug_id}", plug_type=plug_type, printer_id=None)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Enough of AsyncSession for the two helpers under test."""

    def __init__(self, plugs):
        self._plugs = plugs

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._plugs)


@pytest.mark.asyncio
async def test_main_energy_path_asks_the_resolver():
    """A plug type the chain predates must not quietly land on Tasmota."""
    from backend.app import main

    driver = SimpleNamespace(get_energy=AsyncMock(return_value={"total": 1.5}))
    with patch.object(main.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)) as resolver:
        result = await main._get_plug_energy(_plug("some-future-type"), db=None)

    resolver.assert_awaited_once()
    driver.get_energy.assert_awaited_once()
    assert result == {"total": 1.5}


class TestTheEndOfAPrintAsksThePlugItself:
    """A cached reading is not good enough for the end of an energy measurement.

    The counter only moves when the plug reports it, and it is asked to report
    at most every 30 s. A print that has just stopped drawing 200 W therefore
    leaves up to half a minute of consumption out of its own archive — every
    time, and always in the same direction, so it does not average out.
    """

    @pytest.mark.asyncio
    async def test_a_cache_backed_driver_is_told_to_go_and_look(self):
        from backend.app import main

        driver = SimpleNamespace(
            reads_from_a_cache=True,
            refresh=AsyncMock(return_value=True),
            get_energy=AsyncMock(return_value={"total": 3.0}),
        )
        with patch.object(main.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
            result = await main._get_plug_energy(_plug("zigbee"), db=None, force_read=True)

        driver.refresh.assert_awaited_once()
        assert result == {"total": 3.0}

    @pytest.mark.asyncio
    async def test_a_driver_that_reads_live_is_left_alone(self):
        """Tasmota, REST and Home Assistant make an HTTP call per question, and
        the MQTT driver holds what the plug pushed. There is nothing to force,
        and a driver without the attribute must not blow up on the flag."""
        from backend.app import main

        driver = SimpleNamespace(get_energy=AsyncMock(return_value={"total": 3.0}))
        with patch.object(main.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
            result = await main._get_plug_energy(_plug("tasmota"), db=None, force_read=True)

        assert result == {"total": 3.0}

    @pytest.mark.asyncio
    async def test_an_ordinary_read_does_not_touch_the_radio(self):
        """Everything else — status cards, the snapshot loop — keeps taking the
        cache. Forcing a read per viewer is the pile-up this driver was fixed
        for once already."""
        from backend.app import main

        driver = SimpleNamespace(
            reads_from_a_cache=True,
            refresh=AsyncMock(return_value=True),
            get_energy=AsyncMock(return_value={"total": 3.0}),
        )
        with patch.object(main.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
            await main._get_plug_energy(_plug("zigbee"), db=None)

        driver.refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_end_of_print_handler_actually_passes_the_flag(self):
        """The flag is only worth having if the one caller that needs it sets it.

        Driving the real function rather than asserting on the source: a keyword
        dropped in a refactor would leave every archive short by whatever the
        printer drew in the last half-minute, with nothing failing anywhere.
        """
        from contextlib import asynccontextmanager

        from backend.app import main

        archive = SimpleNamespace(energy_start_kwh=1.0, energy_kwh=None, energy_cost=None)
        db = SimpleNamespace(get=AsyncMock(return_value=archive), commit=AsyncMock())

        @asynccontextmanager
        async def fake_session():
            yield db

        seen = {}

        async def fake_energy(plug, db, *, force_read=False):
            seen["force_read"] = force_read
            return {"total": 1.25}

        with (
            patch.object(main, "async_session", fake_session),
            patch.object(main, "_energy_plug_for_printer", AsyncMock(return_value=_plug("zigbee"))),
            patch.object(main, "_get_plug_energy", fake_energy),
            patch.object(main.smart_plug_manager, "record_energy_snapshot", AsyncMock()),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="0.20")),
        ):
            await main._record_print_energy(archive_id=7, printer_id=3)

        assert seen["force_read"] is True
        assert archive.energy_kwh == 0.25, "and the delta is still computed from it"

    def test_the_zigbee_driver_is_the_one_that_declares_a_cache(self):
        """Pins the flag to the driver rather than to the test's own stub."""
        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService

        assert ZigbeeSmartPlugService.reads_from_a_cache is True


@pytest.mark.asyncio
async def test_archive_total_asks_the_resolver():
    """A plug type the chain predates must contribute, not be skipped."""
    from backend.app.api.routes import archives

    driver = SimpleNamespace(get_energy=AsyncMock(return_value={"total": 2.0}))
    with patch.object(archives.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
        total = await archives._sum_live_plug_totals(_FakeDB([_plug("some-future-type")]))

    assert total == 2.0


@pytest.mark.asyncio
async def test_archive_total_still_reads_rest_daily_figure():
    """REST reports only ``today``; everything else reports a lifetime ``total``.

    That asymmetry predates this change and is real — a REST plug genuinely has
    no lifetime counter. Collapsing both onto one key while removing the
    per-type chain would silently drop REST plugs out of the totals.
    """
    from backend.app.api.routes import archives

    driver = SimpleNamespace(get_energy=AsyncMock(return_value={"today": 0.75}))
    with patch.object(archives.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
        total = await archives._sum_live_plug_totals(_FakeDB([_plug("rest")]))

    assert total == 0.75


@pytest.mark.asyncio
async def test_archive_total_ignores_a_driver_that_returns_nothing():
    """An unreachable plug contributes zero rather than raising."""
    from backend.app.api.routes import archives

    driver = SimpleNamespace(get_energy=AsyncMock(return_value=None))
    with patch.object(archives.smart_plug_manager, "get_service_for_plug", AsyncMock(return_value=driver)):
        total = await archives._sum_live_plug_totals(_FakeDB([_plug("mqtt")]))

    assert total == 0.0


class TestUnknownPlugTypeIsLoud:
    """A plug type no driver claims must fail, not become Tasmota.

    ``get_service_for_plug`` used to end in a bare ``return tasmota_service``.
    That is not a fallback — it is a wrong answer delivered confidently: an HTTP
    poll aimed at an ``ip_address`` the plug does not have, feeding a number
    into per-print energy. It stayed harmless only because every shipping type
    happened to be listed; m113 had to add ``mqtt`` for exactly this reason, and
    ``zigbee`` would have been the next one.
    """

    @pytest.mark.asyncio
    async def test_an_unknown_type_raises_instead_of_defaulting(self):
        from backend.app.services.smart_plug_manager import (
            UnknownPlugTypeError,
            smart_plug_manager,
        )

        plug = MagicMock()
        plug.plug_type = "some_future_plug"

        with pytest.raises(UnknownPlugTypeError, match="some_future_plug"):
            await smart_plug_manager.get_service_for_plug(plug, None)

    @pytest.mark.asyncio
    async def test_tasmota_is_reached_by_name_not_by_falling_through(self):
        """The point of the change: Tasmota is now a branch like any other, so
        it can only be selected deliberately."""
        from backend.app.services import smart_plug_manager as mod

        plug = MagicMock()
        plug.plug_type = "tasmota"

        assert await mod.smart_plug_manager.get_service_for_plug(plug, None) is mod.tasmota_service

    @pytest.mark.asyncio
    async def test_every_shipping_type_still_resolves(self):
        """Guards the other direction: making the mapping total must not drop a
        type that real rows carry. These are exactly the values the SmartPlug
        schema accepts."""
        from backend.app.services.smart_plug_manager import smart_plug_manager

        for plug_type in ("tasmota", "rest", "mqtt", "zigbee"):
            plug = MagicMock()
            plug.plug_type = plug_type
            assert await smart_plug_manager.get_service_for_plug(plug, None) is not None, plug_type

    @pytest.mark.asyncio
    async def test_one_unresolvable_plug_does_not_stop_the_schedule_pass(self):
        """The schedule loop drives power for the whole farm and had no
        per-plug guard, so a single bad row would abort the pass and every plug
        after it silently missed its schedule — a printer left on overnight
        because an unrelated plug was misconfigured.
        """
        from backend.app.services.smart_plug_manager import SmartPlugManager

        bad, good = MagicMock(), MagicMock()
        for i, (p, ptype) in enumerate(((bad, "some_future_plug"), (good, "tasmota"))):
            p.plug_type, p.id, p.name = ptype, i + 1, ptype
            p.printer_id = None
            p.last_state = "OFF"
            p.schedule_on_time = "08:00"
            p.schedule_off_time = "22:00"

        with (
            patch("backend.app.services.smart_plug_manager.datetime") as mock_datetime,
            patch("backend.app.core.database.async_session") as mock_session_ctx,
            patch("backend.app.services.smart_plug_manager.tasmota_service") as mock_tasmota,
        ):
            now = MagicMock()
            now.strftime.return_value = "08:00"
            mock_datetime.now.return_value = now

            db = AsyncMock()
            result = MagicMock()
            # The unresolvable plug comes FIRST: before the guard it aborted the
            # pass here, and the good plug never got its turn.
            result.scalars.return_value.all.return_value = [bad, good]
            db.execute = AsyncMock(return_value=result)
            db.commit = AsyncMock()
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock()

            mock_tasmota.turn_on = AsyncMock(return_value=True)

            await SmartPlugManager()._check_schedules()

            mock_tasmota.turn_on.assert_called_once_with(good)
