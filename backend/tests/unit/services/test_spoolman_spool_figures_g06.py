"""A Spoolman spool's size, tare, consumed counter and price, read from the
spool itself (upstream #2906, #3194, #3195).

* The consumed-counter reset records a baseline in ``spool.extra`` and leaves
  every native Spoolman number alone — zeroing ``used_weight`` put the spool
  back to full, because Spoolman derives ``remaining_weight`` from it.
* The net (label) weight is the spool's own ``initial_weight`` before the
  filament's ``weight``.
* The tare follows Spoolman's own order: spool, filament, vendor, 250 g.
* ``price`` is what the spool cost; the form's cost per kg is that price at
  the spool's own net weight.
"""

import asyncio
import json

import pytest

from backend.app.api.routes._spoolman_helpers import (
    BAMBU_WEIGHT_USED_BASELINE_KEY,
    _map_spoolman_spool,
    cost_per_kg_to_spoolman_price,
    spoolman_cost_per_kg,
    spoolman_tare,
)
from backend.app.services import spoolman as spoolman_service
from backend.app.services.spoolman_weights import spool_price_per_gram
from backend.tests.unit.services.spoolman_weighing_fake import WeighingSpoolman, client_for


def _spool(**fields) -> dict:
    filament = {
        "id": 99,
        "name": "PLA Black",
        "material": "PLA",
        "color_hex": "000000",
        "weight": 1000,
        "vendor": {"id": 3, "name": "Bambu Lab"},
    }
    filament.update(fields.pop("filament", {}))
    return {"id": 7, "filament": filament, "extra": {}, "registered": "2026-01-01T00:00:00Z", **fields}


def _consumed(mapped: dict) -> float:
    return mapped["weight_used"] - mapped["weight_used_baseline"]


class TestStoredBaseline:
    def test_a_reset_spool_reads_zero_consumed_and_keeps_its_remainder(self):
        mapped = _map_spoolman_spool(
            _spool(used_weight=456.0, remaining_weight=544.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: '"456.0"'})
        )
        assert _consumed(mapped) == 0
        assert mapped["label_weight"] - mapped["weight_used"] == 544.0

    def test_use_after_a_reset_counts_from_the_baseline(self):
        mapped = _map_spoolman_spool(
            _spool(used_weight=500.0, remaining_weight=500.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: '"456.0"'})
        )
        assert _consumed(mapped) == 44.0

    def test_a_baseline_above_the_used_weight_reads_zero_not_negative(self):
        # An AMS weight sync raised the remainder after the reset.
        mapped = _map_spoolman_spool(
            _spool(used_weight=300.0, remaining_weight=700.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: '"456.0"'})
        )
        assert _consumed(mapped) == 0
        assert mapped["weight_used"] == 300.0

    @pytest.mark.parametrize("stored", ['"200.0"', "200.0", "200", 200, 200.0])
    def test_every_spelling_of_the_stored_number_reads_the_same(self, stored):
        mapped = _map_spoolman_spool(
            _spool(used_weight=456.0, remaining_weight=544.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: stored})
        )
        assert mapped["weight_used_baseline"] == 200.0

    @pytest.mark.parametrize("stored", ['"-50"', '"abc"', '""', "not json", True, None, '"nan"'])
    def test_an_unusable_stored_value_is_no_baseline(self, stored):
        mapped = _map_spoolman_spool(
            _spool(used_weight=456.0, remaining_weight=544.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: stored})
        )
        assert mapped["weight_used_baseline"] == 0.0

    def test_a_spool_without_a_remaining_weight_still_subtracts_the_baseline(self):
        mapped = _map_spoolman_spool(_spool(used_weight=123.0, extra={BAMBU_WEIGHT_USED_BASELINE_KEY: '"100.0"'}))
        assert _consumed(mapped) == 23.0

    def test_the_client_and_the_mapper_name_the_same_key(self):
        assert spoolman_service.BAMBU_WEIGHT_USED_BASELINE_KEY == BAMBU_WEIGHT_USED_BASELINE_KEY


class TestResetWritesOnlyTheBaseline:
    def _fake(self) -> WeighingSpoolman:
        fake = WeighingSpoolman(registered={"tag"})
        fake.add_filament(99, name="PLA Black", material="PLA", weight=1000)
        fake.add_spool(7, 99, used_weight=456.0, extra={"tag": '"AABB"'})
        return fake

    @pytest.mark.asyncio
    async def test_no_native_weight_is_written(self):
        fake = self._fake()
        answer = await client_for(fake).reset_spool_consumed_counter(7)

        (body,) = fake.bodies("PATCH", "/spool/7")
        assert set(body) == {"extra"}
        assert fake.spools[7]["used_weight"] == 456.0
        assert fake.spools[7]["initial_weight"] == 1000
        assert answer["remaining_weight"] == 544.0
        assert _consumed(_map_spoolman_spool(answer)) == 0

    @pytest.mark.asyncio
    async def test_the_baseline_is_a_json_string_and_other_extras_survive(self):
        fake = self._fake()
        await client_for(fake).reset_spool_consumed_counter(7)

        extra = fake.spools[7]["extra"]
        assert json.loads(extra[BAMBU_WEIGHT_USED_BASELINE_KEY]) == "456.0"
        assert extra["tag"] == '"AABB"'

    @pytest.mark.asyncio
    async def test_a_concurrent_extra_write_is_not_lost(self):
        fake = self._fake()
        client = client_for(fake)
        await asyncio.gather(
            client.reset_spool_consumed_counter(7),
            client.merge_spool_extra(7, {"bambu_color_name": json.dumps("Black")}),
        )
        extra = fake.spools[7]["extra"]
        assert json.loads(extra[BAMBU_WEIGHT_USED_BASELINE_KEY]) == "456.0"
        assert json.loads(extra["bambu_color_name"]) == "Black"

    def test_zeroing_used_weight_is_gone_from_the_client(self):
        assert not hasattr(spoolman_service.SpoolmanClient, "reset_spool_usage")


class TestNetWeight:
    def test_the_spools_own_weight_is_its_label_weight(self):
        mapped = _map_spoolman_spool(_spool(initial_weight=250, used_weight=50.0, remaining_weight=200.0))
        assert mapped["label_weight"] == 250
        assert mapped["weight_used"] == 50.0
        assert mapped["weight_used_baseline"] == 0.0

    @pytest.mark.parametrize("initial", [0, None, True])
    def test_an_unset_own_weight_falls_back_to_the_filament(self, initial):
        mapped = _map_spoolman_spool(_spool(initial_weight=initial, used_weight=0.0, remaining_weight=1000.0))
        assert mapped["label_weight"] == 1000

    def test_no_weight_anywhere_keeps_the_old_default(self):
        mapped = _map_spoolman_spool(_spool(filament={"weight": None}, used_weight=0.0))
        assert mapped["label_weight"] == 1000


class TestTare:
    def test_the_spools_own_tare_wins(self):
        spool = _spool(spool_weight=180, filament={"spool_weight": 200, "vendor": {"empty_spool_weight": 216}})
        assert spoolman_tare(spool) == 180.0

    def test_then_the_filaments(self):
        assert spoolman_tare(_spool(filament={"spool_weight": 200, "vendor": {"empty_spool_weight": 216}})) == 200.0

    def test_then_the_vendors(self):
        spool = _spool(filament={"vendor": {"name": "Bambu Lab", "empty_spool_weight": 216}})
        assert spoolman_tare(spool) == 216.0
        assert _map_spoolman_spool(spool)["core_weight"] == 216

    def test_then_250_grams(self):
        assert spoolman_tare(_spool()) == 250.0

    def test_a_zero_tare_on_the_spool_is_a_real_tare(self):
        assert spoolman_tare(_spool(spool_weight=0, filament={"spool_weight": 200})) == 0.0


class TestCost:
    def test_a_spool_price_reads_as_a_rate_at_the_spools_own_size(self):
        spool = _spool(initial_weight=250, price=12)
        mapped = _map_spoolman_spool(spool)
        assert mapped["cost_per_kg"] == 48.0
        assert mapped["effective_cost_per_kg"] == 48.0

    def test_a_one_kilogram_spool_reads_as_before(self):
        assert _map_spoolman_spool(_spool(price=20))["cost_per_kg"] == 20.0

    def test_a_spool_priced_through_its_filament_keeps_no_rate_of_its_own(self):
        mapped = _map_spoolman_spool(_spool(filament={"price": 25}))
        assert mapped["cost_per_kg"] is None
        assert mapped["effective_cost_per_kg"] == 25.0

    def test_a_spool_level_zero_is_charged_the_filament_price(self):
        mapped = _map_spoolman_spool(_spool(price=0, filament={"price": 25}))
        assert mapped["cost_per_kg"] == 0.0
        assert mapped["effective_cost_per_kg"] == 25.0

    def test_no_price_anywhere_is_unpriced(self):
        mapped = _map_spoolman_spool(_spool())
        assert mapped["cost_per_kg"] is None
        assert mapped["effective_cost_per_kg"] is None

    @pytest.mark.parametrize(
        "spool",
        [
            _spool(initial_weight=250, price=12),
            _spool(price=19.99),
            _spool(price=0, filament={"price": 25}),
            _spool(initial_weight=750.5, filament={"price": 31.4}),
        ],
    )
    def test_the_shown_rate_is_the_charge_rule(self, spool):
        assert _map_spoolman_spool(spool)["effective_cost_per_kg"] == round(spool_price_per_gram(spool) * 1000.0, 4)

    def test_a_price_without_a_size_keeps_the_one_to_one_reading(self):
        assert spoolman_cost_per_kg(_spool(price=20, filament={"weight": None})) == 20.0

    @pytest.mark.parametrize(("price", "net"), [(12.0, 250.0), (19.99, 1000.0), (7.35, 750.5), (33.33, 3000.0)])
    def test_a_rate_round_trips_to_the_same_price(self, price, net):
        spool = _spool(initial_weight=net, price=price)
        assert cost_per_kg_to_spoolman_price(spoolman_cost_per_kg(spool), net) == pytest.approx(price, abs=1e-3)


class TestClientWritesTheSpoolsSize:
    def _fake(self) -> WeighingSpoolman:
        fake = WeighingSpoolman()
        fake.add_filament(99, name="PLA Black", material="PLA", weight=1000)
        return fake

    @pytest.mark.asyncio
    async def test_create_sends_the_initial_weight_it_is_given(self):
        fake = self._fake()
        spool = await client_for(fake).create_spool(filament_id=99, remaining_weight=250.0, initial_weight=250.0)
        assert fake.bodies("POST", "/spool")[0]["initial_weight"] == 250.0
        assert spool["remaining_weight"] == 250.0
        assert spool["used_weight"] == 0.0

    @pytest.mark.asyncio
    async def test_create_without_one_leaves_the_size_to_spoolman(self):
        fake = self._fake()
        await client_for(fake).create_spool(filament_id=99, remaining_weight=900.0)
        assert "initial_weight" not in fake.bodies("POST", "/spool")[0]

    @pytest.mark.asyncio
    async def test_update_sends_the_initial_weight(self):
        fake = self._fake()
        fake.add_spool(7, 99, used_weight=100.0)
        updated = await client_for(fake).update_spool_full(7, initial_weight=250.0, remaining_weight=150.0)
        assert updated["initial_weight"] == 250.0
        assert updated["remaining_weight"] == 150.0
