"""A stored print cost is a snapshot; re-pricing is an explicit, scoped action with a preview.

Owner's decision Р4 (upstream 4f70d3da6, adapted — option C). The correction
case: a spool bought at 20 /kg was entered as 200 /kg, and every print from it
was costed tenfold. "Recalculate Costs" re-sums the stored rows, so it cannot
fix that. Re-pricing rewrites the cost of the usage rows of ONE spool and / or
ONE period at the spool's current price, moves each archive by exactly the
difference, and says beforehand how many archives and orders will move.

It never touches grams: what a print was charged in filament stays as booked.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.project import Project
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

URL = "/api/v1/statistics/reprice-usage"


async def _spool(db, cost_per_kg, weight_used=0.0) -> Spool:
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, cost_per_kg=cost_per_kg, weight_used=weight_used)
    db.add(spool)
    await db.flush()
    return spool


async def _row(db, spool, archive, grams, cost, *, at=None, status="completed") -> SpoolUsageHistory:
    row = SpoolUsageHistory(
        spool_id=spool.id,
        printer_id=archive.printer_id if archive else None,
        print_name=archive.print_name if archive else None,
        archive_id=archive.id if archive else None,
        weight_used=grams,
        percent_used=int(grams / 10),
        status=status,
        cost=cost,
        created_at=at or datetime(2026, 9, 10, 12, 0, 0),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def mistyped(db_session, printer_factory, archive_factory):
    """Ten 100 g prints from a spool entered as 200 /kg (real price 20), five of them under one order."""
    printer = await printer_factory()
    order = Project(name="Lamp order", status="active")
    db_session.add(order)
    await db_session.flush()
    spool = await _spool(db_session, 200.0, weight_used=1000.0)
    archives, rows = [], []
    for index in range(10):
        archive = await archive_factory(
            printer.id,
            print_name=f"Print {index}",
            filament_used_grams=100.0,
            cost=20.0,
            energy_kwh=0.4,
            energy_cost=0.12,
            project_id=order.id if index < 5 else None,
        )
        archives.append(archive)
        rows.append(await _row(db_session, spool, archive, 100.0, 20.0))
    await db_session.commit()
    return {"printer": printer, "order": order, "spool": spool, "archives": archives, "rows": rows}


async def _fresh(db_session, model, ids):
    result = await db_session.execute(
        select(model).where(model.id.in_(ids)).order_by(model.id).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.integration
class TestRepricingOneSpool:
    async def test_the_preview_says_what_would_move_and_writes_nothing(
        self, async_client: AsyncClient, db_session, mistyped
    ):
        spool_id = mistyped["spool"].id
        archive_ids = [a.id for a in mistyped["archives"]]
        row_ids = [r.id for r in mistyped["rows"]]
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()

        response = await async_client.post(URL, json={"spool_id": spool_id, "dry_run": True})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert (body["rows"], body["rows_changed"]) == (10, 10)
        assert (body["archives_changed"], body["orders_changed"]) == (10, 1)
        assert (body["cost_before"], body["cost_after"]) == (200.0, 20.0)
        assert {a.cost for a in await _fresh(db_session, PrintArchive, archive_ids)} == {20.0}
        assert {r.cost for r in await _fresh(db_session, SpoolUsageHistory, row_ids)} == {20.0}

    async def test_applying_rewrites_the_rows_and_moves_each_archive_by_the_difference(
        self, async_client: AsyncClient, db_session, mistyped
    ):
        spool_id = mistyped["spool"].id
        archive_ids = [a.id for a in mistyped["archives"]]
        row_ids = [r.id for r in mistyped["rows"]]
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()

        response = await async_client.post(URL, json={"spool_id": spool_id, "dry_run": False})

        assert response.status_code == 200, response.text
        assert response.json()["archives_changed"] == 10
        archives = await _fresh(db_session, PrintArchive, archive_ids)
        rows = await _fresh(db_session, SpoolUsageHistory, row_ids)
        assert {a.cost for a in archives} == {2.0}
        assert {r.cost for r in rows} == {2.0}

    async def test_the_filament_a_print_was_charged_and_its_energy_do_not_move(
        self, async_client: AsyncClient, db_session, mistyped
    ):
        spool_id = mistyped["spool"].id
        archive_ids = [a.id for a in mistyped["archives"]]
        row_ids = [r.id for r in mistyped["rows"]]
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()

        await async_client.post(URL, json={"spool_id": spool_id, "dry_run": False})

        archives = await _fresh(db_session, PrintArchive, archive_ids)
        rows = await _fresh(db_session, SpoolUsageHistory, row_ids)
        spool = (await _fresh(db_session, Spool, [spool_id]))[0]
        assert {r.weight_used for r in rows} == {100.0}
        assert spool.weight_used == 1000.0
        assert {a.filament_used_grams for a in archives} == {100.0}
        assert {(a.energy_kwh, a.energy_cost) for a in archives} == {(0.4, 0.12)}

    async def test_a_second_run_changes_nothing(self, async_client: AsyncClient, db_session, mistyped):
        spool_id = mistyped["spool"].id
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()
        await async_client.post(URL, json={"spool_id": spool_id, "dry_run": False})

        again = (await async_client.post(URL, json={"spool_id": spool_id, "dry_run": False})).json()

        assert (again["rows_changed"], again["archives_changed"], again["orders_changed"]) == (0, 0, 0)

    async def test_a_print_from_two_spools_keeps_the_other_spools_share_and_the_default_share(
        self, async_client: AsyncClient, db_session, printer_factory, archive_factory
    ):
        """20.00 (mistyped spool) + 1.50 (another spool) + 0.50 (grams no spool covered) = 22.00 → 4.00."""
        printer = await printer_factory()
        wrong = await _spool(db_session, 200.0)
        other = await _spool(db_session, 30.0)
        archive = await archive_factory(printer.id, filament_used_grams=170.0, cost=22.0)
        wrong_row = await _row(db_session, wrong, archive, 100.0, 20.0)
        other_row = await _row(db_session, other, archive, 50.0, 1.5)
        wrong.cost_per_kg = 20.0
        other.cost_per_kg = 90.0  # re-priced for a new purchase: not in the selection, must not move
        await db_session.commit()
        ids = (archive.id, wrong_row.id, other_row.id, wrong.id)

        body = (await async_client.post(URL, json={"spool_id": ids[3], "dry_run": False})).json()

        assert (body["rows"], body["rows_changed"], body["archives_changed"]) == (1, 1, 1)
        assert (await _fresh(db_session, PrintArchive, [ids[0]]))[0].cost == 4.0
        rows = {r.id: r.cost for r in await _fresh(db_session, SpoolUsageHistory, ids[1:3])}
        assert rows == {ids[1]: 2.0, ids[2]: 1.5}

    async def test_a_spool_without_a_price_takes_the_default_rate(
        self, async_client: AsyncClient, db_session, printer_factory, archive_factory
    ):
        from backend.app.models.settings import Settings

        printer = await printer_factory()
        db_session.add(Settings(key="default_filament_cost", value="25"))
        spool = await _spool(db_session, None)
        archive = await archive_factory(printer.id, filament_used_grams=100.0, cost=20.0)
        row = await _row(db_session, spool, archive, 100.0, 20.0)
        await db_session.commit()
        ids = (spool.id, archive.id, row.id)

        await async_client.post(URL, json={"spool_id": ids[0], "dry_run": False})

        assert (await _fresh(db_session, SpoolUsageHistory, [ids[2]]))[0].cost == 2.5
        assert (await _fresh(db_session, PrintArchive, [ids[1]]))[0].cost == 2.5


@pytest.mark.asyncio
@pytest.mark.integration
class TestRepricingAPeriod:
    async def test_only_rows_of_the_period_move(
        self, async_client: AsyncClient, db_session, printer_factory, archive_factory
    ):
        """Spring prints at 18 /kg keep their cost when the record is re-priced to 26 for autumn."""
        printer = await printer_factory()
        spool = await _spool(db_session, 26.0)
        spring = await archive_factory(printer.id, print_name="Spring", filament_used_grams=100.0, cost=1.8)
        autumn = await archive_factory(printer.id, print_name="Autumn", filament_used_grams=100.0, cost=1.8)
        spring_row = await _row(db_session, spool, spring, 100.0, 1.8, at=datetime(2026, 4, 2, 9, 0, 0))
        autumn_row = await _row(db_session, spool, autumn, 100.0, 1.8, at=datetime(2026, 10, 3, 9, 0, 0))
        await db_session.commit()
        ids = (spool.id, spring.id, autumn.id, spring_row.id, autumn_row.id)

        body = (
            await async_client.post(
                URL,
                json={"spool_id": ids[0], "date_from": "2026-10-01", "date_to": "2026-10-31", "dry_run": False},
            )
        ).json()

        assert (body["rows"], body["rows_changed"], body["archives_changed"]) == (1, 1, 1)
        costs = {a.id: a.cost for a in await _fresh(db_session, PrintArchive, ids[1:3])}
        assert costs == {ids[1]: 1.8, ids[2]: 2.6}

    async def test_a_period_alone_is_a_scope(self, async_client: AsyncClient, db_session, mistyped):
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()

        body = (
            await async_client.post(URL, json={"date_from": "2026-09-10", "date_to": "2026-09-10", "dry_run": True})
        ).json()

        assert (body["rows"], body["archives_changed"], body["orders_changed"]) == (10, 10, 1)


@pytest.mark.asyncio
@pytest.mark.integration
class TestRepricingIsNeverUnscoped:
    async def test_no_spool_and_no_period_is_refused(self, async_client: AsyncClient, mistyped):
        response = await async_client.post(URL, json={"dry_run": True})

        assert response.status_code == 422

    async def test_half_a_period_is_refused(self, async_client: AsyncClient, mistyped):
        response = await async_client.post(URL, json={"date_from": "2026-09-10", "dry_run": True})

        assert response.status_code == 422

    async def test_an_unknown_spool_is_404(self, async_client: AsyncClient):
        response = await async_client.post(URL, json={"spool_id": 987654, "dry_run": True})

        assert response.status_code == 404

    async def test_applying_is_never_the_default(self, async_client: AsyncClient, db_session, mistyped):
        """A call that does not say ``dry_run: false`` only previews."""
        spool_id = mistyped["spool"].id
        row_ids = [r.id for r in mistyped["rows"]]
        mistyped["spool"].cost_per_kg = 20.0
        await db_session.commit()

        body = (await async_client.post(URL, json={"spool_id": spool_id})).json()

        assert body["dry_run"] is True
        assert {r.cost for r in await _fresh(db_session, SpoolUsageHistory, row_ids)} == {20.0}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recalculate_costs_still_keeps_recorded_prices(async_client: AsyncClient, db_session, mistyped):
    """The old button is unchanged: it re-sums stored rows and cannot fix a mistyped price."""
    archive_ids = [a.id for a in mistyped["archives"]]
    mistyped["spool"].cost_per_kg = 20.0
    await db_session.commit()

    response = await async_client.post("/api/v1/statistics/recalculate-costs")

    assert response.status_code == 200
    assert {a.cost for a in await _fresh(db_session, PrintArchive, archive_ids)} == {20.0}
