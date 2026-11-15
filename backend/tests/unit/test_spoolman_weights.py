"""The two pure rules the shown figures and the charge share (upstream #3194).

``spool_net_weight`` — the net (label) weight of a Spoolman spool: the spool's
own ``initial_weight``, else its filament's catalogue ``weight``.
``spool_price_per_gram`` — what a gram off that spool costs: the spool's own
``price``, else the filament's, over that net weight.
"""

import pytest

from backend.app.services.spoolman_weights import spool_net_weight, spool_price_per_gram


def _spool(*, initial=None, weight=None, price=None, filament_price=None) -> dict:
    spool: dict = {"id": 1, "filament": {"id": 2}}
    if initial is not None:
        spool["initial_weight"] = initial
    if weight is not None:
        spool["filament"]["weight"] = weight
    if price is not None:
        spool["price"] = price
    if filament_price is not None:
        spool["filament"]["price"] = filament_price
    return spool


class TestNetWeight:
    def test_the_spools_own_weight_wins_over_the_filaments(self):
        assert spool_net_weight(_spool(initial=250, weight=1000)) == 250.0

    def test_a_spool_without_its_own_weight_takes_the_filaments(self):
        assert spool_net_weight(_spool(weight=1000)) == 1000.0

    @pytest.mark.parametrize("initial", [0, 0.0, -5, None, True, "abc", float("nan"), float("inf")])
    def test_an_unusable_own_weight_falls_back_to_the_filament(self, initial):
        spool = _spool(weight=750)
        spool["initial_weight"] = initial
        assert spool_net_weight(spool) == 750.0

    @pytest.mark.parametrize("weight", [0, -1, None, True, "x", float("nan")])
    def test_no_usable_weight_at_either_level_is_none(self, weight):
        spool = _spool()
        spool["filament"]["weight"] = weight
        assert spool_net_weight(spool) is None

    def test_numbers_sent_as_text_are_read(self):
        assert spool_net_weight(_spool(initial="250.5", weight=1000)) == 250.5

    @pytest.mark.parametrize("spool", [None, {}, {"filament": None}, {"filament": "x"}])
    def test_a_missing_spool_or_filament_is_none(self, spool):
        assert spool_net_weight(spool) is None


class TestPricePerGram:
    def test_a_spool_price_is_divided_by_the_spools_own_weight(self):
        # 12 for a 250 g spool of a 1000 g filament: 48 per kg, not 12.
        assert spool_price_per_gram(_spool(initial=250, weight=1000, price=12)) == pytest.approx(0.048)

    def test_a_spool_without_a_price_takes_the_filaments(self):
        assert spool_price_per_gram(_spool(weight=1000, filament_price=25)) == pytest.approx(0.025)

    @pytest.mark.parametrize("price", [0, 0.0, -3, None, True, "x", float("nan")])
    def test_an_unusable_spool_price_falls_back_to_the_filament(self, price):
        spool = _spool(weight=1000, filament_price=30)
        spool["price"] = price
        assert spool_price_per_gram(spool) == pytest.approx(0.03)

    def test_the_filament_price_is_divided_by_the_same_net_weight(self):
        assert spool_price_per_gram(_spool(initial=500, weight=1000, filament_price=20)) == pytest.approx(0.04)

    def test_no_price_anywhere_is_unpriced(self):
        assert spool_price_per_gram(_spool(weight=1000)) is None

    def test_a_price_without_a_weight_is_unpriced(self):
        assert spool_price_per_gram(_spool(price=20)) is None

    @pytest.mark.parametrize("spool", [None, {}])
    def test_a_missing_spool_is_unpriced(self, spool):
        assert spool_price_per_gram(spool) is None
