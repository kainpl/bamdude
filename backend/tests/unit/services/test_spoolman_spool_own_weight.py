"""A Spoolman spool is charged and priced by ITS OWN net weight (upstream 2f7f17a89, #3194).

A 250 g refill registered under a 1000 g catalogue filament: the no-3MF path turns
the AMS remain percentage into grams, and a gram into money, with the catalogue's
1000 g — four times the real usage, and a quarter of the real price per gram.
A spool that states no weight of its own is charged exactly as before.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.spoolman_tracking import (
    _PrintCost,
    _report_remain_delta_for_slots,
    _spool_cost_per_gram,
)


def _spool(*, initial_weight=None, price=None, weight=1000, filament_price=None):
    spool = {"id": 41, "price": price, "filament": {"weight": weight, "price": filament_price}}
    if initial_weight is not None:
        spool["initial_weight"] = initial_weight
    return spool


async def _remain_delta(spool, *, start=80, end=60):
    """AMS0-T0 dropped from ``start`` % to ``end`` % over a print with no 3MF."""
    client = AsyncMock()
    client.get_spool = AsyncMock(return_value=spool)
    cost = _PrintCost()
    with patch(
        "backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment",
        AsyncMock(return_value=spool["id"]),
    ):
        updated = await _report_remain_delta_for_slots(
            client,
            printer_id=1,
            tray_remain_start={"0-0": {"remain": start, "tray_uuid": ""}},
            current_lookup={"0-0": {"remain": end, "tray_uuid": ""}},
            handled_global_tray_ids=set(),
            archive_id=5,
            cost_out=cost,
        )
    charged = [(call.args[0], call.args[1]) for call in client.use_spool.await_args_list]
    return updated, charged, cost


class TestRemainDeltaUsesTheSpoolsOwnWeight:
    @pytest.mark.asyncio
    async def test_a_250g_refill_under_a_1000g_filament_is_charged_its_own_grams(self):
        """20 % of a 250 g spool is 50 g — not the 200 g that 20 % of the catalogue's 1000 g gives."""
        updated, charged, cost = await _remain_delta(_spool(initial_weight=250, price=8.0))
        assert updated == 1
        assert charged == [(41, 50.0)]
        assert cost.charged_grams == pytest.approx(50.0)
        # 8.00 for 250 g is 0.032 per gram: 50 g cost 1.60 (not 200 g x 0.008 = 1.60 by accident
        # of two errors cancelling — the grams above are the proof, the rate is checked apart).
        assert cost.cost == pytest.approx(1.6)

    @pytest.mark.asyncio
    async def test_a_spool_without_its_own_weight_is_charged_as_before(self):
        """GUARD: no ``initial_weight`` — 20 % of the filament's 1000 g, 200 g at 25.00 / kg."""
        updated, charged, cost = await _remain_delta(_spool(filament_price=25.0))
        assert updated == 1
        assert charged == [(41, 200.0)]
        assert cost.cost == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_a_spool_with_no_weight_at_any_level_is_skipped(self):
        updated, charged, _cost = await _remain_delta(_spool(weight=None))
        assert (updated, charged) == (0, [])

    @pytest.mark.asyncio
    async def test_an_own_weight_alone_is_enough_to_charge(self):
        """A spool whose filament carries no catalogue weight was skipped; its own weight charges it."""
        updated, charged, _cost = await _remain_delta(_spool(initial_weight=500, weight=None), start=50, end=40)
        assert (updated, charged) == (1, [(41, 50.0)])


class TestPricePerGramUsesTheSpoolsOwnWeight:
    def test_a_250g_spool_bought_for_8_costs_0_032_per_gram(self):
        assert _spool_cost_per_gram(_spool(initial_weight=250, price=8.0)) == pytest.approx(0.032)

    def test_the_catalogue_price_is_for_the_filaments_own_weight(self):
        """A filament's price is what a full spool of ``filament.weight`` costs: a 250 g spool of that
        filament costs the same per gram, not four times as much."""
        assert _spool_cost_per_gram(_spool(initial_weight=250, filament_price=25.0)) == pytest.approx(0.025)

    def test_without_its_own_weight_the_rate_is_unchanged(self):
        assert _spool_cost_per_gram(_spool(price=40.0, filament_price=25.0)) == pytest.approx(0.04)
        assert _spool_cost_per_gram(_spool(filament_price=30.0, weight=750)) == pytest.approx(0.04)
