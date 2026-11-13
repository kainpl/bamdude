"""Cost evidence belongs to an execution, never to a reusable print name."""

import pytest
from sqlalchemy import delete, select

from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

pytestmark = pytest.mark.asyncio


async def setup_costs(db, *, rate="20", spoolman=False):
    await db.execute(delete(Settings).where(Settings.key.in_(["default_filament_cost", "spoolman_enabled"])))
    db.add(Settings(key="spoolman_enabled", value=str(spoolman).lower()))
    if rate is not None:
        db.add(Settings(key="default_filament_cost", value=rate))
    spool = Spool(material="PLA", weight_used=300)
    db.add(spool)
    await db.commit()
    return spool


async def archive(db, *, name="Repeated", grams=100, cost=None):
    row = PrintArchive(
        filename="same.gcode.3mf",
        file_path="",
        file_size=1024,
        print_name=name,
        status="completed",
        filament_used_grams=grams,
        cost=cost,
    )
    db.add(row)
    await db.commit()
    return row


async def usage_snapshot(db):
    return (await db.execute(select(SpoolUsageHistory.__table__).order_by(SpoolUsageHistory.id))).all()


@pytest.mark.parametrize("name", ["Repeated", None])
@pytest.mark.parametrize("spoolman", [False, True])
async def test_unlinked_same_name_cannot_price_two_runs(async_client, db_session, name, spoolman):
    spool = await setup_costs(db_session, spoolman=spoolman)
    first = await archive(db_session, name=name, cost=7)
    second = await archive(db_session, name=name, grams=200)
    db_session.add(SpoolUsageHistory(spool_id=spool.id, print_name=name, weight_used=300, cost=99))
    await db_session.commit()
    before = await usage_snapshot(db_session)

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert (first.cost, second.cost) == ((7, 4) if spoolman else (2, 4))
    assert response.json()["updated"] == (1 if spoolman else 2)
    assert response.json()["preserved"] == (1 if spoolman else 0)
    assert await usage_snapshot(db_session) == before  # No retro-link or history mutation.

    again = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert again.status_code == 200, again.text
    assert again.json()["updated"] == 0


@pytest.mark.parametrize("status", ["completed", "failed", "legacy_outcome"])
async def test_archive_link_beats_name_and_service_corrections(async_client, db_session, status):
    spool = await setup_costs(db_session, spoolman=True)
    first = await archive(db_session, cost=40)
    second = await archive(db_session, grams=200, cost=50)
    db_session.add_all(
        [
            SpoolUsageHistory(
                spool_id=spool.id, archive_id=first.id, print_name="Renamed", status=status, weight_used=30, cost=5
            ),
            SpoolUsageHistory(spool_id=spool.id, archive_id=second.id, print_name="Another", weight_used=200, cost=9),
            SpoolUsageHistory(spool_id=spool.id, archive_id=first.id, status="runout", weight_used=900, cost=900),
            SpoolUsageHistory(spool_id=spool.id, archive_id=first.id, status="ams_sync", weight_used=800, cost=800),
            SpoolUsageHistory(spool_id=spool.id, print_name=first.print_name, weight_used=300, cost=99),
        ]
    )
    await db_session.commit()
    before = await usage_snapshot(db_session)

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.cost == pytest.approx(6.4)  # 5 linked + 70 untracked grams at 20/kg.
    assert second.cost == 9
    assert response.json()["updated"] == 2 and response.json()["preserved"] == 0
    assert await usage_snapshot(db_session) == before


@pytest.mark.parametrize("cost", [0, 7])
async def test_unlinked_usage_cannot_replace_spoolman_cost_even_zero(async_client, db_session, cost):
    spool = await setup_costs(db_session, spoolman=True)
    row = await archive(db_session, cost=cost)
    db_session.add(SpoolUsageHistory(spool_id=spool.id, print_name=row.print_name, weight_used=100, cost=99))
    await db_session.commit()

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(row)
    assert row.cost == cost
    assert response.json()["updated"] == 0 and response.json()["preserved"] == 1


@pytest.mark.parametrize(
    "rate,grams,expected", [(None, 100, None), ("0", 100, None), ("20", None, None), ("20", 0, None), ("20", 125, 2.5)]
)
async def test_unlinked_usage_does_not_replace_configured_estimate_policy(
    async_client, db_session, rate, grams, expected
):
    spool = await setup_costs(db_session, rate=rate)
    row = await archive(db_session, grams=grams)
    db_session.add(SpoolUsageHistory(spool_id=spool.id, print_name=row.print_name, weight_used=100, cost=99))
    await db_session.commit()

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(row)
    assert row.cost == expected
    assert response.json()["updated"] == int(expected is not None)


async def test_service_only_links_are_not_pricing_evidence(async_client, db_session):
    spool = await setup_costs(db_session)
    row = await archive(db_session)
    db_session.add_all(
        [
            SpoolUsageHistory(spool_id=spool.id, archive_id=row.id, status=status, weight_used=900, cost=900)
            for status in ("runout", "ams_sync")
        ]
    )
    await db_session.commit()

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(row)
    assert row.cost == 2
