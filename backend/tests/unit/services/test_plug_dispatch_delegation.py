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

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
