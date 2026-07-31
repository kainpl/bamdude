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
