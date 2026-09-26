"""A print in Spoolman mode is priced — and weighed — from the spools that fed it
(upstream 39835437, #2591; the grams half is audit D6's carry-over).

Spoolman holds per-spool prices, and nothing read them: the archive kept the
figure ``archive.py`` computed at print start, and the per-spool recompute in
``usage_tracker`` never runs in Spoolman mode, because ``spoolman_owns_usage``
stops it writing rows. Multi-material was wrong twice over: one rate applied to
the whole print's grams. And the archive's filament figure never learned what
Spoolman was actually charged — a print whose 3MF never arrived stayed at 0 g,
a failed one at the full estimate — which the internal writer has always done
through ``actual_filament_grams``.

Each charge that lands is recorded (grams, and money when the spool has a price);
at the end the archive's grams follow the same rule as the internal writer and
the cost is the priced part plus the rest at the farm rate.
"""

from __future__ import annotations

import inspect
import re

import pytest

from backend.app.services import spoolman_tracking
from backend.app.services.spoolman_tracking import (
    _apply_spool_charges_to_archive,
    _PrintCost,
    _report_spool_usage_for_slots,
    _spool_cost_per_gram,
)


def _spool(spool_id, *, price=None, filament_price=None, weight=1000, color="888888", material="PLA"):
    """A Spoolman spool row, shaped as its API returns one."""
    return {
        "id": spool_id,
        "price": price,
        "filament": {"price": filament_price, "weight": weight, "color_hex": color, "material": material},
    }


class TestSpoolCostPerGram:
    def test_uses_the_filament_catalogue_price(self):
        assert _spool_cost_per_gram(_spool(1, filament_price=25.0, weight=1000)) == pytest.approx(0.025)

    def test_the_spools_own_price_overrides_the_filaments(self):
        assert _spool_cost_per_gram(_spool(1, price=40.0, filament_price=25.0, weight=1000)) == pytest.approx(0.04)

    def test_weight_is_net_filament_grams_not_a_fixed_kilo(self):
        assert _spool_cost_per_gram(_spool(1, filament_price=30.0, weight=750)) == pytest.approx(0.04)

    def test_a_zero_spool_override_falls_through_to_the_catalogue(self):
        """Importers write 0 for "not set" often enough that reading it as free
        would price a print at the default rate with a real price one level down."""
        assert _spool_cost_per_gram(_spool(1, price=0, filament_price=25.0, weight=1000)) == pytest.approx(0.025)

    @pytest.mark.parametrize(
        "spool",
        [
            _spool(1, weight=1000),  # no price anywhere
            _spool(1, filament_price=25.0, weight=None),  # no reference weight
            _spool(1, filament_price=0, weight=1000),  # zero is unpriced, not free
            _spool(1, filament_price=-5, weight=1000),
            _spool(1, filament_price="abc", weight=1000),
            {"id": 1, "price": 25, "filament": "not a dict"},
            {"id": 1, "price": 25, "filament": {"weight": True}},  # bool is an int in Python
            {"id": 1, "price": float("nan"), "filament": {"weight": 1000}},
            {"id": 1, "price": 1e308, "filament": {"weight": 1e-308}},  # the quotient overflows
            None,
        ],
    )
    def test_says_nothing_rather_than_guessing(self, spool):
        assert _spool_cost_per_gram(spool) is None


class TestPrintCostAccumulator:
    def test_sums_each_slot_at_its_own_rate(self):
        cost = _PrintCost()
        cost.add(100.0, _spool(1, filament_price=20.0, weight=1000), "slot 1")  # 0.02/g
        cost.add(50.0, _spool(2, filament_price=60.0, weight=1000), "slot 2")  # 0.06/g
        assert cost.cost == pytest.approx(5.0)
        assert cost.priced_grams == pytest.approx(150.0)
        assert cost.charged_grams == pytest.approx(150.0)
        assert cost.priced == 2

    def test_an_unpriced_spool_is_weighed_but_not_priced(self):
        cost = _PrintCost()
        cost.add(100.0, _spool(1, filament_price=20.0, weight=1000), "slot 1")
        cost.add(50.0, _spool(2, weight=1000), "slot 2")
        assert cost.cost == pytest.approx(2.0)
        assert cost.priced_grams == pytest.approx(100.0)
        assert cost.charged_grams == pytest.approx(150.0), "every charged gram counts toward the archive's weight"
        assert (cost.priced, cost.unpriced) == (1, 1)

    def test_zero_grams_is_not_a_slot(self):
        cost = _PrintCost()
        cost.add(0.0, _spool(1, filament_price=20.0, weight=1000), "slot 1")
        assert (cost.priced, cost.unpriced, cost.cost, cost.charged_grams) == (0, 0, 0.0, 0.0)


async def _archive(db_session, *, grams, cost):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=None,
        filename="a.3mf",
        file_path="",
        file_size=0,
        print_name="a",
        filament_used_grams=grams,
        cost=cost,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _rate(db_session, value: str | None):
    from sqlalchemy import delete

    from backend.app.models.settings import Settings

    await db_session.execute(delete(Settings).where(Settings.key == "default_filament_cost"))
    if value is not None:
        db_session.add(Settings(key="default_filament_cost", value=value))
    await db_session.commit()


async def _apply(db_session, archive, charges, status):
    await _apply_spool_charges_to_archive(db_session, archive.id, charges, status=status)
    await db_session.refresh(archive)
    return archive


class TestTheArchiveLearnsWhatWasCharged:
    @pytest.mark.asyncio
    async def test_the_linked_spools_price_replaces_the_estimate(self, db_session):
        """The reported bug: 100 g off a spool that cost 40.00 per kg is 4.00."""
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(100.0, _spool(41, price=40.0), "slot 1")
        archive = await _apply(db_session, await _archive(db_session, grams=100.0, cost=2.5), charges, "completed")
        assert archive.cost == pytest.approx(4.0)
        assert archive.filament_used_grams == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_multi_material_bills_each_slot_at_its_own_price(self, db_session):
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(100.0, _spool(41, filament_price=20.0), "slot 1")
        charges.add(50.0, _spool(42, filament_price=60.0), "slot 2")
        archive = await _apply(db_session, await _archive(db_session, grams=150.0, cost=3.75), charges, "completed")
        assert archive.cost == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_grams_no_spool_priced_are_covered_at_the_farm_rate(self, db_session):
        """One priced slot out of a heavier print must not report only its share."""
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(100.0, _spool(41, price=40.0), "slot 1")
        archive = await _apply(db_session, await _archive(db_session, grams=150.0, cost=3.75), charges, "completed")
        assert archive.cost == pytest.approx(5.25)

    @pytest.mark.asyncio
    async def test_nothing_priced_and_the_same_grams_leaves_the_cost_alone(self, db_session):
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(100.0, _spool(41), "slot 1")
        archive = await _apply(db_session, await _archive(db_session, grams=100.0, cost=2.5), charges, "completed")
        assert archive.cost == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_a_print_with_no_estimate_takes_the_charged_weight(self, db_session):
        """No 3MF, no estimate: Spoolman's remain%-delta charge is the only
        weight there is, as it is for the internal writer (audit D6)."""
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(80.0, _spool(41), "AMS0-T0")
        archive = await _apply(db_session, await _archive(db_session, grams=None, cost=None), charges, "completed")
        assert archive.filament_used_grams == pytest.approx(80.0)
        assert archive.cost == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_with_no_rate_an_unpriced_weight_gets_no_invented_cost(self, db_session):
        await _rate(db_session, None)
        charges = _PrintCost()
        charges.add(80.0, _spool(41), "AMS0-T0")
        archive = await _apply(db_session, await _archive(db_session, grams=None, cost=None), charges, "completed")
        assert archive.filament_used_grams == pytest.approx(80.0)
        assert archive.cost is None

    @pytest.mark.asyncio
    async def test_a_failed_print_is_weighed_and_priced_at_what_it_used(self, db_session):
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(40.0, _spool(41, price=40.0), "Partial slot 1")
        archive = await _apply(db_session, await _archive(db_session, grams=100.0, cost=2.5), charges, "failed")
        assert archive.filament_used_grams == pytest.approx(40.0)
        assert archive.cost == pytest.approx(1.6)

    @pytest.mark.asyncio
    async def test_a_failed_print_with_no_prices_keeps_the_rate_it_was_costed_at(self, db_session):
        """Nothing priced, fewer grams: the recorded cost priced the estimate at
        whatever rate archive.py chose, so it scales with the weight."""
        await _rate(db_session, "25")
        charges = _PrintCost()
        charges.add(40.0, _spool(41), "Partial slot 1")
        archive = await _apply(db_session, await _archive(db_session, grams=100.0, cost=3.0), charges, "failed")
        assert archive.filament_used_grams == pytest.approx(40.0)
        assert archive.cost == pytest.approx(1.2)

    @pytest.mark.asyncio
    async def test_nothing_charged_changes_nothing(self, db_session):
        await _rate(db_session, "25")
        archive = await _apply(db_session, await _archive(db_session, grams=100.0, cost=2.5), _PrintCost(), "failed")
        assert (archive.filament_used_grams, archive.cost) == (100.0, 2.5)


class _Client:
    def __init__(self, spools: dict[str, dict], *, fail: bool = False):
        self._by_tag = spools
        self._fail = fail
        self.charged: list[tuple[int, float]] = []

    async def find_spool_by_tag(self, tag):
        return self._by_tag.get(tag)

    async def get_spool(self, spool_id):
        return next((s for s in self._by_tag.values() if s["id"] == spool_id), _spool(spool_id, price=10.0))

    async def use_spool(self, spool_id, grams):
        if self._fail:
            from backend.app.services.spoolman import SpoolmanUnavailableError

            raise SpoolmanUnavailableError("down")
        self.charged.append((spool_id, grams))


class TestTheChargePathsPriceWhatLanded:
    @pytest.mark.asyncio
    async def test_a_tag_resolved_charge_is_priced_from_the_same_fetch(self, monkeypatch):
        monkeypatch.setattr(spoolman_tracking, "_resolve_spool_tag", lambda *_a: "TRAY0")
        client = _Client({"TRAY0": _spool(41, price=40.0)})
        charges = _PrintCost()
        await _report_spool_usage_for_slots(
            client, [(1, 100.0)], {0: {"tray_type": "PLA"}}, [0], "Archive 7", cost_out=charges
        )
        assert client.charged == [(41, 100.0)]
        assert charges.cost == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_a_slot_assignment_charge_fetches_its_spool_for_the_price(self, monkeypatch):
        monkeypatch.setattr(spoolman_tracking, "_resolve_spool_tag", lambda *_a: "")

        async def _assigned(*_a):
            return 55

        monkeypatch.setattr(spoolman_tracking, "_resolve_spool_id_via_slot_assignment", _assigned)
        client = _Client({})
        charges = _PrintCost()
        await _report_spool_usage_for_slots(
            client, [(1, 50.0)], {0: {"tray_type": "PLA"}}, [0], "Archive 7", printer_id=1, cost_out=charges
        )
        assert client.charged == [(55, 50.0)]
        assert charges.cost == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_a_refused_charge_is_neither_weighed_nor_priced(self, monkeypatch):
        monkeypatch.setattr(spoolman_tracking, "_resolve_spool_tag", lambda *_a: "TRAY0")
        client = _Client({"TRAY0": _spool(41, price=40.0)}, fail=True)
        charges = _PrintCost()
        await _report_spool_usage_for_slots(
            client, [(1, 100.0)], {0: {"tray_type": "PLA"}}, [0], "Archive 7", cost_out=charges
        )
        assert (charges.charged_grams, charges.cost) == (0.0, 0.0)


class TestTheWiring:
    @staticmethod
    def _source(fn) -> str:
        return re.sub(r"\s+", " ", inspect.getsource(fn))

    @pytest.mark.parametrize(
        "fn",
        [
            spoolman_tracking._report_spool_usage_for_slots,
            spoolman_tracking._report_spool_usage_split_by_tray_changes,
            spoolman_tracking._report_journal_splits,
            spoolman_tracking._report_remain_delta_for_slots,
        ],
    )
    def test_every_charge_path_records_what_it_charged(self, fn):
        assert "cost_out.add(" in self._source(fn)

    def test_the_completion_writer_settles_the_archive(self):
        source = self._source(spoolman_tracking.report_usage)
        assert source.count("cost_out=print_cost") >= 4
        assert 'await _settle_charges(db, archive_id, print_cost, status="completed")' in source

    def test_the_partial_writer_settles_the_archive(self):
        source = self._source(spoolman_tracking._report_partial_usage)
        assert source.count("cost_out=print_cost") >= 3
        assert "_settle_charges(" in source

    def test_runout_zero_corrections_are_not_the_prints_consumption(self):
        """Lifetime drift closing out a spool must not inflate what the print
        weighed or cost — the internal writer keeps it out the same way."""
        assert "cost_out" not in self._source(spoolman_tracking._apply_runout_zero_corrections_spoolman)
