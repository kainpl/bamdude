"""A print's charges to Spoolman spools are recorded as usage history (upstream 1d86cff5a, #3407).

Spoolman keeps a spool's totals and no per-print log, so until now a Spoolman
farm could not see which print took how much from which spool. Every charge that
LANDS writes one row — observed where the charge is already recorded for the
archive (``_PrintCost.add``), never a second accounting: the grams sent to
Spoolman and the cost settled on the archive are the same with and without the
history, and a history that cannot be written changes neither.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.services import spoolman_tracking, spoolman_usage_history as history
from backend.app.services.spoolman_tracking import (
    _PrintCost,
    _report_remain_delta_for_slots,
    _report_spool_usage_for_slots,
    _settle_charges,
)
from backend.tests.unit.services.test_spoolman_print_cost_2591 import _archive, _Client, _rate, _spool

REGISTERED = "2026-10-01T08:00:00Z"


def _full(spool_id, **kwargs):
    """A Spoolman spool row with the identity fields the list shows."""
    spool = _spool(spool_id, **kwargs)
    spool["registered"] = REGISTERED
    spool["filament"].update({"name": "PLA Matte", "vendor": {"name": "Sunlu"}})
    return spool


async def _spoolman(db_session, url="http://spoolman.local:7912"):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value=url))
    await db_session.commit()


async def _rows(db_session) -> list[SpoolmanUsageHistory]:
    return list(
        (await db_session.execute(select(SpoolmanUsageHistory).order_by(SpoolmanUsageHistory.id))).scalars().all()
    )


async def _charge_two_slots(monkeypatch, db_session, *, fail=False, status="completed"):
    """One print: 100 g off a 40.00/kg spool (tag) and 50 g off an unpriced one (tag)."""
    await _rate(db_session, "20")
    archive = await _archive(db_session, grams=150.0, cost=3.0)
    tags = {0: "TRAY0", 1: "TRAY1"}
    monkeypatch.setattr(spoolman_tracking, "_resolve_spool_tag", lambda _info, _serial, tray: tags[tray])
    client = _Client({"TRAY0": _full(41, price=40.0), "TRAY1": _full(42)}, fail=fail)
    charges = _PrintCost(printer_id=None, archive_id=archive.id, history_status=status)
    await _report_spool_usage_for_slots(
        client,
        [(1, 100.0), (2, 50.0)],
        {0: {"tray_type": "PLA"}, 1: {"tray_type": "PLA"}},
        [0, 1],
        "Archive",
        cost_out=charges,
    )
    await _settle_charges(db_session, archive.id, charges, status="completed")
    await db_session.refresh(archive)
    return client, archive, charges


class TestEveryLandedChargeIsARow:
    @pytest.mark.asyncio
    async def test_a_completed_print_writes_one_row_per_charged_spool(self, monkeypatch, db_session):
        await _spoolman(db_session)
        _client, archive, _charges = await _charge_two_slots(monkeypatch, db_session)

        rows = await _rows(db_session)
        assert [(r.spoolman_spool_id, r.weight_used, r.cost, r.status) for r in rows] == [
            (41, 100.0, 4.0, "completed"),
            (42, 50.0, None, "completed"),  # unpriced: a row, and no invented cost
        ]
        assert {r.archive_id for r in rows} == {archive.id}
        assert {r.print_name for r in rows} == {"a"}
        assert rows[0].percent_used == 10  # 100 g of a 1000 g spool
        # The spool as the list shows it, kept on the row.
        assert (rows[0].material, rows[0].subtype, rows[0].brand, rows[0].rgba) == ("PLA", "Matte", "Sunlu", "888888FF")
        assert rows[0].spool_registered == REGISTERED
        assert rows[0].created_at is not None and rows[0].created_at.tzinfo is None

    @pytest.mark.asyncio
    async def test_a_charge_spoolman_refused_writes_nothing(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session, fail=True)
        assert await _rows(db_session) == []

    @pytest.mark.asyncio
    async def test_a_stopped_print_carries_its_own_status(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session, status="cancelled")
        assert {r.status for r in await _rows(db_session)} == {"cancelled"}

    @pytest.mark.asyncio
    async def test_a_charge_whose_spool_was_not_handed_over_is_still_history(self, db_session):
        """The price fetch failed, the charge landed: grams and spool id are known."""
        await _spoolman(db_session)
        charges = _PrintCost(printer_id=None, archive_id=None)
        charges.add(12.5, None, "journal", spool_id=77)
        await _settle_charges(db_session, None, charges, status="completed")
        (row,) = await _rows(db_session)
        assert (row.spoolman_spool_id, row.weight_used, row.cost, row.material, row.spool_registered) == (
            77,
            12.5,
            None,
            None,
            None,
        )

    @pytest.mark.asyncio
    async def test_the_remain_delta_path_names_its_spool(self, monkeypatch, db_session):
        await _spoolman(db_session)

        async def _assigned(*_a):
            return 55

        monkeypatch.setattr(spoolman_tracking, "_resolve_spool_id_via_slot_assignment", _assigned)
        client = _Client({"X": _full(55, price=30.0)})
        charges = _PrintCost(printer_id=None, archive_id=None)
        await _report_remain_delta_for_slots(
            client,
            printer_id=1,
            tray_remain_start={"0-0": {"remain": 50, "tray_uuid": ""}},
            current_lookup={"0-0": {"remain": 40, "tray_uuid": ""}},
            handled_global_tray_ids=set(),
            archive_id=-1,
            cost_out=charges,
        )
        await _settle_charges(db_session, None, charges, status="completed")
        (row,) = await _rows(db_session)
        assert (row.spoolman_spool_id, row.weight_used, row.cost) == (55, 100.0, 3.0)


class TestTheChargeIsOnlyObserved:
    """GUARD: what Spoolman is charged and what the archive records do not depend on the history."""

    @pytest.mark.asyncio
    async def test_grams_and_cost_are_the_same_with_and_without_the_history(self, monkeypatch, db_session):
        await _spoolman(db_session)
        client, archive, charges = await _charge_two_slots(monkeypatch, db_session)
        with_history = (
            client.charged,
            archive.filament_used_grams,
            archive.cost,
            charges.charged_grams,
            charges.cost,
            charges.priced_grams,
            charges.priced,
            charges.unpriced,
        )
        assert len(await _rows(db_session)) == 2

        async def _boom(**_kwargs):
            raise RuntimeError("history table is gone")

        monkeypatch.setattr(history, "record_print", _boom)
        client, archive, charges = await _charge_two_slots(monkeypatch, db_session)
        without_history = (
            client.charged,
            archive.filament_used_grams,
            archive.cost,
            charges.charged_grams,
            charges.cost,
            charges.priced_grams,
            charges.priced,
            charges.unpriced,
        )

        assert with_history == without_history
        # 100 g at 40.00/kg + 50 g at the 20.00/kg farm rate; hand-worked.
        assert with_history == ([(41, 100.0), (42, 50.0)], 150.0, 5.0, 150.0, 4.0, 100.0, 1, 1)
        assert len(await _rows(db_session)) == 2, "the failed write added nothing and removed nothing"

    def test_the_accumulator_reads_the_same_with_and_without_a_spool_id(self):
        plain, named = _PrintCost(), _PrintCost()
        for cost, kwargs in ((plain, {}), (named, {"spool_id": 41})):
            cost.add(100.0, _spool(41, price=40.0), "slot 1", **kwargs)
            cost.add(50.0, _spool(42), "slot 2", **kwargs)
            cost.add(0.0, _spool(43, price=10.0), "slot 3", **kwargs)
        figures = ("charged_grams", "cost", "priced_grams", "priced", "unpriced")
        assert [getattr(plain, f) for f in figures] == [getattr(named, f) for f in figures]
        assert [(c.spool_id, c.grams, c.rate) for c in named.observed] == [(41, 100.0, 0.04), (41, 50.0, None)]
        assert plain.observed == [], "a charge that names no spool is not history"


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
    def test_every_charge_path_names_the_spool_it_charged(self, fn):
        source = self._source(fn)
        assert source.count("cost_out.add(") == 1
        assert re.search(r"cost_out\.add\([^;]*?spool_id=", source)

    def test_runout_zero_corrections_are_not_print_usage(self):
        source = self._source(spoolman_tracking._apply_runout_zero_corrections_spoolman)
        assert "cost_out" not in source and "spoolman_usage_history" not in source

    def test_both_writers_tell_the_accumulator_which_print_it_is(self):
        assert "_PrintCost(printer_id=printer_id, archive_id=archive_id" in self._source(spoolman_tracking.report_usage)
        assert "_PrintCost(printer_id=printer_id, archive_id=partial_archive_id" in self._source(
            spoolman_tracking._report_partial_usage
        )


class TestTheSourceToken:
    @pytest.mark.asyncio
    async def test_no_address_means_no_spoolman_to_name(self, db_session):
        assert await history.source_token(db_session) is None

    @pytest.mark.asyncio
    async def test_the_token_is_stable_for_one_address(self, db_session):
        await _spoolman(db_session, "http://Spoolman.local:7912/")
        first = await history.source_token(db_session)
        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = "http://spoolman.local:7912"  # the same address, written differently
        await db_session.commit()
        assert first and await history.source_token(db_session) == first

    @pytest.mark.asyncio
    async def test_another_address_is_another_spoolman(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        old = await history.source_token(db_session)

        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = "http://other-host:7912"
        await db_session.commit()
        new = await history.source_token(db_session)

        assert new and new != old
        assert {r.source_token for r in await _rows(db_session)} == {old}, "the old rows stay, under the old source"
        state = await history.source_state(db_session)
        assert (state["rows"], state["previous_rows"], state["previous_url"]) == (0, 2, "http://spoolman.local:7912")

    @pytest.mark.asyncio
    async def test_a_cleared_address_keeps_the_token(self, db_session):
        await _spoolman(db_session)
        token = await history.source_token(db_session)
        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = ""
        await db_session.commit()
        assert await history.source_token(db_session) == token

    @pytest.mark.asyncio
    async def test_the_operator_can_say_it_is_the_same_database(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = "http://renamed-host:7912"
        await db_session.commit()

        assert await history.keep_previous_source(db_session) == 2
        token = await history.source_token(db_session)
        assert {r.source_token for r in await _rows(db_session)} == {token}
        assert (await history.source_state(db_session))["previous_rows"] == 0

    @pytest.mark.asyncio
    async def test_the_operator_can_say_it_is_a_new_database(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        old = await history.source_token(db_session)
        await history.rotate_source(db_session)
        assert await history.source_token(db_session) != old
        assert (await history.source_state(db_session))["rows"] == 0

    async def _move_to(self, db_session, url: str) -> str:
        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = url
        await db_session.commit()
        return await history.source_token(db_session)

    @pytest.mark.asyncio
    async def test_going_back_to_a_known_address_finds_its_history_again(self, monkeypatch, db_session):
        """A → B → A: a mistyped host, or https tried and undone, with any reader
        running once in between, orphaned every row for good (review accounting I1)."""
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        first = await history.source_token(db_session)

        assert await self._move_to(db_session, "https://spoolman.local:7912") != first
        assert await self._move_to(db_session, "http://spoolman.local:7912/") == first

        state = await history.source_state(db_session)
        assert (state["rows"], state["previous_rows"], state["previous_url"]) == (2, 0, None)

    @pytest.mark.asyncio
    async def test_an_empty_stop_on_the_way_does_not_hide_a_previous_source_with_rows(self, monkeypatch, db_session):
        """A → B → C with nothing recorded at B: the card still offers A's history."""
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)

        await self._move_to(db_session, "http://typo:7912")
        await self._move_to(db_session, "http://new-host:7912")

        state = await history.source_state(db_session)
        assert (state["previous_rows"], state["previous_url"]) == (2, "http://spoolman.local:7912")
        assert await history.keep_previous_source(db_session) == 2

    @pytest.mark.asyncio
    async def test_a_history_kept_for_a_renamed_host_is_there_under_the_old_name_too(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        renamed = await self._move_to(db_session, "http://renamed-host:7912")
        await history.keep_previous_source(db_session)

        assert await self._move_to(db_session, "http://spoolman.local:7912") == renamed
        assert (await history.source_state(db_session))["rows"] == 2

    @pytest.mark.asyncio
    async def test_a_database_declared_new_stays_new_after_a_round_trip(self, monkeypatch, db_session):
        await _spoolman(db_session)
        await _charge_two_slots(monkeypatch, db_session)
        await history.rotate_source(db_session)
        fresh = await history.source_token(db_session)

        await self._move_to(db_session, "http://elsewhere:7912")
        assert await self._move_to(db_session, "http://spoolman.local:7912") == fresh
        assert (await history.source_state(db_session))["rows"] == 0


class TestWhichSpoolARowBelongsTo:
    def test_the_same_registration_is_the_same_spool(self):
        assert history.belongs_to(REGISTERED, datetime(2026, 10, 5), "2026-10-01T08:00:00+00:00")

    def test_a_spool_registered_later_under_the_id_is_another_spool(self):
        assert not history.belongs_to(REGISTERED, datetime(2026, 10, 5), "2026-11-01T08:00:00Z")

    def test_a_row_without_a_stamp_is_judged_by_its_age(self):
        registered = "2026-10-10T00:00:00Z"
        assert not history.belongs_to(None, datetime(2026, 10, 10) - timedelta(seconds=1), registered)
        assert history.belongs_to(None, datetime(2026, 10, 10), registered)

    def test_nothing_to_compare_keeps_the_row(self):
        assert history.belongs_to(None, datetime(2026, 10, 5), None)
        assert history.belongs_to(REGISTERED, datetime(2026, 10, 5), "not a time")
