"""A zigbee plug must be reachable through the one resolver everything uses.

This is where phase 1's investment is checked. Both energy paths were rewired
then — main.py::_get_plug_energy ended its chain with ``else: tasmota_service``
and the archive sum had no ``else`` at all — precisely so that adding a plug
type would be one line in the manager rather than four edits across the app.
If these tests pass without touching main.py or archives.py, that paid off.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.services.zigbee.driver import ZigbeeSmartPlugService, zigbee_smart_plug_service


def _plug(plug_type="zigbee", plug_id=1):
    return SimpleNamespace(id=plug_id, name="p", plug_type=plug_type, zigbee_ieee="a4:c1:38:0b:5a:9c:ff:ff")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, plugs):
        self._plugs = plugs

    async def execute(self, *_a, **_k):
        return _FakeResult(self._plugs)


@pytest.mark.asyncio
async def test_manager_resolves_the_zigbee_driver():
    service = await smart_plug_manager.get_service_for_plug(_plug(), db=None)

    assert service is zigbee_smart_plug_service


@pytest.mark.asyncio
async def test_the_main_energy_path_reaches_it():
    """No edit to main.py was needed for this — that is the point."""
    from backend.app import main

    with patch.object(zigbee_smart_plug_service, "get_energy", AsyncMock(return_value={"total": 7.5})) as energy:
        result = await main._get_plug_energy(_plug(), db=None)

    energy.assert_awaited_once()
    assert result == {"total": 7.5}


@pytest.mark.asyncio
async def test_the_archive_total_reaches_it():
    from backend.app.api.routes import archives

    with patch.object(zigbee_smart_plug_service, "get_energy", AsyncMock(return_value={"total": 2.25})):
        total = await archives._sum_live_plug_totals(_FakeDB([_plug()]))

    assert total == 2.25


class TestReportingIntoTheCache:
    def test_a_report_updates_the_cache(self):
        service = ZigbeeSmartPlugService()

        service.update(1, state="ON")
        service.update(1, energy_total=12.345)

        data = service.get_plug_data(1)
        assert data.state == "ON"
        assert data.energy_total == pytest.approx(12.345)

    def test_a_later_report_does_not_blank_an_earlier_field(self):
        """Reports arrive per attribute: a power update must not erase the
        energy total that came in a second earlier."""
        service = ZigbeeSmartPlugService()
        service.update(1, state="ON", energy_total=10.0)

        service.update(1, power=42.0)

        data = service.get_plug_data(1)
        assert data.energy_total == pytest.approx(10.0)
        assert data.power == pytest.approx(42.0)

    def test_an_unknown_plug_reads_as_no_data(self):
        """A report for a paired device nobody bound yet is not an error."""
        assert ZigbeeSmartPlugService().get_plug_data(999) is None
