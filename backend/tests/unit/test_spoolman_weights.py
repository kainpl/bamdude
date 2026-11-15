"""The one rule for a Spoolman spool's net weight and its price per gram (upstream 2f7f17a89)."""

import pytest

from backend.app.services.spoolman_weights import spool_net_weight, spool_price_per_gram


def _spool(*, initial_weight=None, price=None, weight=None, filament_price=None):
    spool = {"id": 7, "filament": {}}
    if initial_weight is not None:
        spool["initial_weight"] = initial_weight
    if price is not None:
        spool["price"] = price
    if weight is not None:
        spool["filament"]["weight"] = weight
    if filament_price is not None:
        spool["filament"]["price"] = filament_price
    return spool


class TestSpoolNetWeight:
    def test_the_spools_own_weight_wins_over_the_catalogue(self):
        assert spool_net_weight(_spool(initial_weight=250, weight=1000)) == 250.0

    def test_without_its_own_weight_the_catalogue_stands(self):
        assert spool_net_weight(_spool(weight=1000)) == 1000.0

    @pytest.mark.parametrize("own", [0, -5, None, True, "abc", float("nan"), float("inf")])
    def test_an_unusable_own_weight_falls_back_to_the_catalogue(self, own):
        spool = _spool(weight=750)
        spool["initial_weight"] = own
        assert spool_net_weight(spool) == 750.0

    @pytest.mark.parametrize("weight", [0, -1, None, False, float("nan")])
    def test_no_usable_weight_at_either_level_is_unknown(self, weight):
        spool = _spool()
        spool["filament"]["weight"] = weight
        assert spool_net_weight(spool) is None

    @pytest.mark.parametrize("spool", [None, [], "spool", {"id": 1}, {"id": 1, "filament": None}])
    def test_a_spool_that_states_nothing_is_unknown(self, spool):
        assert spool_net_weight(spool) is None

    def test_an_own_weight_needs_no_filament(self):
        assert spool_net_weight({"id": 1, "initial_weight": 500}) == 500.0


class TestSpoolPricePerGram:
    def test_the_spools_price_over_its_own_weight(self):
        """A 250 g refill bought for 8.00 costs 0.032 per gram, not 8 / 1000."""
        assert spool_price_per_gram(_spool(initial_weight=250, price=8.0, weight=1000)) == pytest.approx(0.032)

    def test_the_catalogue_price_is_divided_by_the_same_net_weight(self):
        assert spool_price_per_gram(_spool(initial_weight=250, filament_price=25.0, weight=1000)) == pytest.approx(0.1)

    def test_a_spool_without_its_own_weight_is_priced_by_the_catalogue_weight(self):
        assert spool_price_per_gram(_spool(filament_price=25.0, weight=1000)) == pytest.approx(0.025)
        assert spool_price_per_gram(_spool(price=40.0, filament_price=25.0, weight=1000)) == pytest.approx(0.04)

    def test_a_zero_spool_price_falls_through_to_the_catalogue(self):
        assert spool_price_per_gram(_spool(price=0, filament_price=25.0, weight=1000)) == pytest.approx(0.025)

    @pytest.mark.parametrize("price", [None, 0, -3, True, float("nan")])
    def test_an_unpriced_spool_has_no_rate(self, price):
        spool = _spool(weight=1000)
        spool["price"] = price
        assert spool_price_per_gram(spool) is None

    def test_a_weightless_spool_has_no_rate(self):
        assert spool_price_per_gram(_spool(price=20.0)) is None

    @pytest.mark.parametrize("spool", [None, 3, {"id": 1}])
    def test_nothing_to_read_is_no_rate(self, spool):
        assert spool_price_per_gram(spool) is None

    def test_an_overflowing_quotient_is_no_rate(self):
        assert spool_price_per_gram(_spool(price=1e308, weight=1e-308)) is None
