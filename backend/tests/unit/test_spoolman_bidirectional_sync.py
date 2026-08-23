"""The Spoolman leg of the bidirectional AMS weight sync (tagged spools only)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.main import _spoolman_bidirectional_weight_sync
from backend.app.services.ams_sync_debounce import DecreaseDebounce

UUID = "AAAA0000AAAA0000AAAA0000AAAA0000"


def _tray(uuid=UUID, tray_id=2):
    return SimpleNamespace(tray_uuid=uuid, tray_id=tray_id)


def _spool(remaining=800.0, weight=1000):
    return {"id": 8, "remaining_weight": remaining, "filament": {"weight": weight}}


def _client():
    client = AsyncMock()
    client.use_spool = AsyncMock()
    client.update_spool = AsyncMock()
    return client


def _fresh_debounce():
    return patch("backend.app.services.ams_sync_debounce.debounce", DecreaseDebounce(window_seconds=60))


@pytest.mark.asyncio
async def test_remaining_drop_is_charged_immediately():
    client = _client()
    with _fresh_debounce(), patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=None)):
        # AMS says 75% of 1000 g = 750 g left; Spoolman book says 800 → 50 g consumed.
        await _spoolman_bidirectional_weight_sync(
            client, AsyncMock(), 1, 0, _tray(), {"remain": 75, "remain_g": -1}, _spool(remaining=800.0)
        )
    client.use_spool.assert_awaited_once_with(8, 50.0)
    client.update_spool.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_rise_needs_two_pushes(monkeypatch):
    client = _client()
    times = iter([100.0, 130.0, 161.0])
    monkeypatch.setattr("backend.app.services.ams_sync_debounce.time", SimpleNamespace(monotonic=lambda: next(times)))
    with _fresh_debounce(), patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=None)):
        args = (client, AsyncMock(), 1, 0, _tray(), {"remain": 90, "remain_g": -1}, _spool(remaining=800.0))
        await _spoolman_bidirectional_weight_sync(*args)  # t=100: candidate
        client.update_spool.assert_not_awaited()
        await _spoolman_bidirectional_weight_sync(*args)  # t=130: too soon
        client.update_spool.assert_not_awaited()
        await _spoolman_bidirectional_weight_sync(*args)  # t=161: confirmed
    client.update_spool.assert_awaited_once_with(8, remaining_weight=900.0)
    client.use_spool.assert_not_awaited()


@pytest.mark.asyncio
async def test_untagged_spool_is_never_touched():
    client = _client()
    with _fresh_debounce(), patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=None)):
        await _spoolman_bidirectional_weight_sync(
            client, AsyncMock(), 1, 0, _tray(uuid="0" * 32), {"remain": 75, "remain_g": -1}, _spool()
        )
    client.use_spool.assert_not_awaited()
    client.update_spool.assert_not_awaited()


@pytest.mark.asyncio
async def test_setting_off_blocks_the_decrease_but_not_the_increase():
    client = _client()
    off = AsyncMock(return_value="false")
    with _fresh_debounce(), patch("backend.app.api.routes.settings.get_setting", off):
        await _spoolman_bidirectional_weight_sync(
            client, AsyncMock(), 1, 0, _tray(), {"remain": 90, "remain_g": -1}, _spool(remaining=800.0)
        )
        client.update_spool.assert_not_awaited()
        await _spoolman_bidirectional_weight_sync(
            client, AsyncMock(), 1, 0, _tray(), {"remain": 75, "remain_g": -1}, _spool(remaining=800.0)
        )
    client.use_spool.assert_awaited_once_with(8, 50.0)


@pytest.mark.asyncio
async def test_unusable_remain_is_refused():
    client = _client()
    with _fresh_debounce(), patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=None)):
        for remain in (0, -1, 101, None):
            await _spoolman_bidirectional_weight_sync(
                client, AsyncMock(), 1, 0, _tray(), {"remain": remain, "remain_g": -1}, _spool()
            )
    client.use_spool.assert_not_awaited()
    client.update_spool.assert_not_awaited()
