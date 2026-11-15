"""Readers outside the inventory routes take a Spoolman spool's own size
(upstream #3194): the automatic AMS weight sync of a tagged spool, and the
printed label."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.main import _spoolman_bidirectional_weight_sync
from backend.app.services.ams_sync_debounce import DecreaseDebounce
from backend.app.services.label_context import spoolman_context

UUID = "AAAA0000AAAA0000AAAA0000AAAA0000"


def _tray():
    return SimpleNamespace(tray_uuid=UUID, tray_id=2)


async def _sync(client, spool, remain):
    with (
        patch("backend.app.services.ams_sync_debounce.debounce", DecreaseDebounce(window_seconds=60)),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=None)),
    ):
        await _spoolman_bidirectional_weight_sync(
            client, AsyncMock(), 1, 0, _tray(), {"remain": remain, "remain_g": -1}, spool
        )


@pytest.mark.asyncio
async def test_the_tagged_spool_sync_converts_percent_at_the_spools_own_size():
    client = AsyncMock()
    # A 250 g refill of a 1000 g filament at 60 %: 150 g left, 50 g gone.
    spool = {"id": 8, "initial_weight": 250, "remaining_weight": 200.0, "filament": {"weight": 1000}}

    await _sync(client, spool, 60)

    client.use_spool.assert_awaited_once_with(8, 50.0)


@pytest.mark.asyncio
async def test_a_spool_without_its_own_size_still_syncs_at_the_filaments():
    client = AsyncMock()

    await _sync(client, {"id": 8, "remaining_weight": 800.0, "filament": {"weight": 1000}}, 75)

    client.use_spool.assert_awaited_once_with(8, 50.0)


@pytest.mark.asyncio
async def test_a_spool_without_any_size_is_left_alone():
    client = AsyncMock()

    await _sync(client, {"id": 8, "remaining_weight": 800.0, "filament": {}}, 75)

    client.use_spool.assert_not_awaited()
    client.update_spool.assert_not_awaited()


def _label(**spool) -> dict:
    payload = {
        "id": 7,
        "used_weight": 50.0,
        "filament": {"name": "PLA Red", "material": "PLA", "weight": 1000, "price": 20},
    }
    payload.update(spool)
    return spoolman_context(payload, deeplink_base="https://bamdude.local")


def test_the_label_prints_the_spools_own_weight():
    context = _label(initial_weight=250)
    assert context["label_weight_g"] == "250"
    assert context["remaining_g"] == "200"


def test_the_label_prints_the_rate_the_spool_is_charged():
    # 12 for this 250 g spool is 48 per kg; the filament's 20 is not its price.
    assert _label(initial_weight=250, price=12)["cost_per_kg"] == "48"


def test_a_label_for_a_spool_priced_through_its_filament_keeps_that_rate():
    assert _label()["cost_per_kg"] == "20"
