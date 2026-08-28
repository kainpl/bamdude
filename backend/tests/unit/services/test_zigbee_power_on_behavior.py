"""Power-on behavior (ZCL StartUpOnOff, 0x4003) on the zigbee plug driver.

Motivation (2026-08-27): a plug dropped off the network for 23 minutes
overnight and came back with the relay OFF — no command from anywhere in the
log. A plug configured to 'previous' cannot do that, so the setting is now
editable from the plug dialog, written to the DEVICE, never stored locally.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.zigbee.driver import ZigbeeSmartPlugService


def _plug(plug_id=4):
    return SimpleNamespace(id=plug_id, name="A1Mini-101 Plug", plug_type="zigbee", zigbee_ieee="aa:bb")


def _service_with_cluster(cluster):
    service = ZigbeeSmartPlugService()
    device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={0x0006: cluster} if cluster else {})})
    service._device_for = MagicMock(return_value=device)  # type: ignore[method-assign]
    return service


class TestReadPowerOnBehavior:
    @pytest.mark.asyncio
    async def test_reads_and_names_the_value(self):
        cluster = MagicMock()
        cluster.read_attributes = AsyncMock(return_value=({0x4003: 255}, {}))
        service = _service_with_cluster(cluster)
        assert await service.read_power_on_behavior(_plug()) == "previous"
        cluster.read_attributes.assert_awaited_once_with([0x4003])

    @pytest.mark.asyncio
    async def test_unsupported_attribute_is_none_not_a_guess(self):
        cluster = MagicMock()
        cluster.read_attributes = AsyncMock(return_value=({}, {0x4003: "UNSUPPORTED_ATTRIBUTE"}))
        service = _service_with_cluster(cluster)
        assert await service.read_power_on_behavior(_plug()) is None

    @pytest.mark.asyncio
    async def test_unreachable_device_is_none(self):
        service = _service_with_cluster(None)
        assert await service.read_power_on_behavior(_plug()) is None

    @pytest.mark.asyncio
    async def test_a_timeout_is_an_answer_not_an_exception(self):
        cluster = MagicMock()
        cluster.read_attributes = AsyncMock(side_effect=TimeoutError("no answer"))
        service = _service_with_cluster(cluster)
        assert await service.read_power_on_behavior(_plug()) is None


class TestWritePowerOnBehavior:
    @pytest.mark.asyncio
    async def test_acknowledged_write_is_true(self):
        cluster = MagicMock()
        cluster.write_attributes = AsyncMock(return_value=[[SimpleNamespace(status=0)]])
        service = _service_with_cluster(cluster)
        assert await service.write_power_on_behavior(_plug(), "previous") is True
        cluster.write_attributes.assert_awaited_once_with({0x4003: 255})

    @pytest.mark.asyncio
    async def test_refused_write_is_false(self):
        cluster = MagicMock()
        cluster.write_attributes = AsyncMock(return_value=[[SimpleNamespace(status=0x86)]])
        service = _service_with_cluster(cluster)
        assert await service.write_power_on_behavior(_plug(), "on") is False

    @pytest.mark.asyncio
    async def test_unknown_mode_never_reaches_the_radio(self):
        cluster = MagicMock()
        cluster.write_attributes = AsyncMock()
        service = _service_with_cluster(cluster)
        assert await service.write_power_on_behavior(_plug(), "toggle") is False
        cluster.write_attributes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreachable_device_is_false(self):
        service = _service_with_cluster(None)
        assert await service.write_power_on_behavior(_plug(), "off") is False
