"""Who reads a print's wear figure: statistics, the archive answer, the printer form, the Git backup.

Tests build rows by hand; in the app only ``services/wear_cost.py`` writes the two archive columns.
"""

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.schemas.printer import PrinterCreate, PrinterUpdate
from backend.app.services.git_backup import git_backup_service
from backend.app.services.git_restore import ARCHIVES_PATH, GitRestoreService, _CategoryTally
from backend.app.services.statistics import aggregate as agg

HASH = "ab" * 32


def _archive(**kwargs) -> PrintArchive:
    defaults = {
        "filename": "part.3mf",
        "file_path": "",
        "file_size": 0,
        "status": "completed",
        "quantity": 1,
        "created_at": datetime(2026, 9, 7, 12, 0),
    }
    return PrintArchive(**{**defaults, **kwargs})


async def _seed(db_session, *rows):
    for row in rows:
        db_session.add(row)
    await db_session.commit()


# --- statistics ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_most_expensive_print_counts_filament_energy_and_wear(db_session):
    await _seed(
        db_session,
        _archive(print_name="filament-heavy", cost=10.0, energy_cost=1.0),
        _archive(print_name="long-run", cost=6.0, energy_cost=2.0, wear_cost=4.5, wear_rate=1.5),
    )

    out = await agg.collect(db_session, tz=ZoneInfo("UTC"), date_from=None, date_to=None, user_id=None)

    record = out["records"]["costliest"]
    assert record["print_name"] == "long-run"  # 12.5 against 11.0: wear decided it
    assert record["total"] == pytest.approx(12.5)
    assert (record["cost"], record["energy_cost"], record["wear_cost"]) == (6.0, 2.0, 4.5)


@pytest.mark.asyncio
async def test_a_print_without_a_wear_figure_competes_on_the_rest(db_session):
    await _seed(db_session, _archive(print_name="plain", cost=3.0, energy_cost=1.0))

    out = await agg.collect(db_session, tz=ZoneInfo("UTC"), date_from=None, date_to=None, user_id=None)

    record = out["records"]["costliest"]
    assert (record["total"], record["wear_cost"]) == (pytest.approx(4.0), 0.0)


@pytest.mark.asyncio
async def test_the_overview_sums_the_stamped_figures(async_client, db_session):
    await _seed(
        db_session,
        _archive(print_name="a", wear_cost=1.25, wear_rate=2.0),
        _archive(print_name="b", status="failed", wear_cost=0.5, wear_rate=2.0),
        _archive(print_name="c"),  # no figure: adds nothing, and is no zero on its own row
    )

    response = await async_client.get("/api/v1/statistics/overview")

    assert response.status_code == 200, response.text
    assert response.json()["total_wear_cost"] == 1.75


@pytest.mark.asyncio
async def test_the_overview_without_any_wear_is_zero(async_client, db_session):
    await _seed(db_session, _archive(print_name="a", cost=1.0))

    response = await async_client.get("/api/v1/statistics/overview")

    assert response.json()["total_wear_cost"] == 0


# --- the archive answer and the printer form -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_archive_answer_carries_the_figure_and_the_rate(async_client, db_session):
    row = _archive(print_name="a", wear_cost=1.25, wear_rate=2.0)
    bare = _archive(print_name="b")
    await _seed(db_session, row, bare)

    stamped = (await async_client.get(f"/api/v1/archives/{row.id}")).json()
    empty = (await async_client.get(f"/api/v1/archives/{bare.id}")).json()

    assert (stamped["wear_cost"], stamped["wear_rate"]) == (1.25, 2.0)
    assert (empty["wear_cost"], empty["wear_rate"]) == (None, None)


def test_the_rate_is_bounded_and_optional():
    base = {"name": "P", "serial_number": "00M09A000000001", "ip_address": "192.0.2.1", "access_code": "12345678"}

    assert PrinterCreate(**base).wear_cost_per_hour is None
    assert PrinterCreate(**base, wear_cost_per_hour=0).wear_cost_per_hour == 0
    assert PrinterCreate(**base, wear_cost_per_hour=2.5).wear_cost_per_hour == 2.5
    for bad in (-0.01, 100000.01):
        with pytest.raises(ValueError):
            PrinterCreate(**base, wear_cost_per_hour=bad)
        with pytest.raises(ValueError):
            PrinterUpdate(wear_cost_per_hour=bad)


def test_an_explicit_null_clears_the_rate():
    # The column is nullable, so the null is kept as sent and the route writes it.
    assert PrinterUpdate(wear_cost_per_hour=None).model_dump(exclude_unset=True) == {"wear_cost_per_hour": None}
    assert "wear_cost_per_hour" not in PrinterUpdate(name="P").model_dump(exclude_unset=True)


@pytest.mark.asyncio
async def test_the_printer_route_sets_and_clears_the_rate(async_client, db_session, printer_factory):
    printer = await printer_factory()

    answer = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"wear_cost_per_hour": 2.5})
    assert answer.status_code == 200, answer.text
    assert answer.json()["wear_cost_per_hour"] == 2.5
    assert (await db_session.scalar(select(Printer.wear_cost_per_hour).where(Printer.id == printer.id))) == 2.5

    refused = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"wear_cost_per_hour": -1})
    assert refused.status_code == 422

    cleared = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"wear_cost_per_hour": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["wear_cost_per_hour"] is None


# --- Git backup and restore ---------------------------------------------------------------------


async def _backup_of(db_session) -> dict:
    files: dict = {}
    await git_backup_service._collect_archives(db_session, files)
    return files[ARCHIVES_PATH]


async def _restore(db_session, payload, *, overwrite=False) -> _CategoryTally:
    tally = _CategoryTally()
    await GitRestoreService()._restore_archives(db_session, payload, overwrite, tally, {})
    await db_session.commit()
    return tally


def _run(**kwargs) -> PrintArchive:
    defaults = {
        "filename": "run.3mf",
        "file_size": 1024,
        "content_hash": HASH,
        "started_at": datetime(2026, 3, 1, 10, 0, 0),
        "completed_at": datetime(2026, 3, 1, 11, 0, 0),
    }
    return _archive(**{**defaults, **kwargs})


@pytest.mark.asyncio
async def test_a_backup_carries_the_pair_and_a_restore_brings_it_back_unchanged(db_session):
    await _seed(db_session, _run(wear_cost=1.234, wear_rate=2.5))
    payload = await _backup_of(db_session)
    assert (payload["archives"][0]["wear_cost"], payload["archives"][0]["wear_rate"]) == (1.234, 2.5)

    await db_session.execute(PrintArchive.__table__.delete())
    await db_session.commit()
    await _restore(db_session, payload)

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    # As recorded: one hour at 2.5 would be 2.5, and nothing recomputed it.
    assert (row.wear_cost, row.wear_rate) == (1.234, 2.5)


@pytest.mark.asyncio
async def test_a_backup_without_a_figure_restores_none(db_session):
    await _seed(db_session, _run())
    payload = await _backup_of(db_session)
    assert (payload["archives"][0]["wear_cost"], payload["archives"][0]["wear_rate"]) == (None, None)

    await db_session.execute(PrintArchive.__table__.delete())
    await db_session.commit()
    await _restore(db_session, payload)

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert (row.wear_cost, row.wear_rate) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("cost", "rate"), [(1.0, None), (None, 2.0), ("1.0", 2.0), (-1.0, 2.0), (1.0, 0)])
async def test_half_a_pair_or_an_unreadable_one_restores_nothing(db_session, cost, rate):
    await _seed(db_session, _run())
    payload = await _backup_of(db_session)
    payload["archives"][0].update(wear_cost=cost, wear_rate=rate)

    await db_session.execute(PrintArchive.__table__.delete())
    await db_session.commit()
    await _restore(db_session, payload)

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert (row.wear_cost, row.wear_rate) == (None, None)


@pytest.mark.asyncio
async def test_a_snapshot_without_a_proven_outcome_brings_no_figure(db_session, printer_factory):
    """A new execution restored as uncertain gets no cost, no energy -- and no wear."""
    printer = await printer_factory()
    entry = {
        "id": 91,
        "filename": "run.3mf",
        "recovery_identity": str(uuid4()),
        "printer_id": printer.id,
        "printer_serial": printer.serial_number,
        "started_at": "2026-03-01T10:00:00",
        "status": "printing",
        "wear_cost": 1.0,
        "wear_rate": 2.0,
    }

    tally = await _restore(db_session, {"archives": [entry]})

    assert tally.restored == 1, (tally.failed, tally.skipped, tally.notes)
    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert (row.extra_data or {}).get("recovered_outcome_uncertain") is True
    assert (row.wear_cost, row.wear_rate) == (None, None)


@pytest.mark.asyncio
async def test_a_restore_never_replaces_the_local_figure_of_the_same_run(db_session):
    await _seed(db_session, _run(wear_cost=1.0, wear_rate=2.0))
    payload = await _backup_of(db_session)
    payload["archives"][0].update(wear_cost=9.0, wear_rate=9.0)

    await _restore(db_session, payload, overwrite=True)

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert (row.wear_cost, row.wear_rate) == (1.0, 2.0)


@pytest.mark.asyncio
async def test_an_overwrite_fills_the_missing_figure_of_the_same_finished_run(db_session):
    await _seed(db_session, _run(wear_cost=1.5, wear_rate=3.0))
    payload = await _backup_of(db_session)
    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    await db_session.execute(PrintArchive.__table__.update().values(wear_cost=None, wear_rate=None))
    await db_session.commit()
    await db_session.refresh(row)

    await _restore(db_session, payload, overwrite=True)

    await db_session.refresh(row)
    assert (row.wear_cost, row.wear_rate) == (1.5, 3.0)


@pytest.mark.asyncio
async def test_a_local_print_still_in_flight_takes_no_figure_from_a_backup(db_session):
    """Its identity rule: an active local execution accepts operator fields only."""
    await _seed(db_session, _run())
    payload = await _backup_of(db_session)
    payload["archives"][0].update(wear_cost=1.0, wear_rate=2.0)
    await db_session.execute(PrintArchive.__table__.update().values(status="printing", completed_at=None))
    await db_session.commit()

    await _restore(db_session, payload, overwrite=True)

    row = (await db_session.execute(select(PrintArchive).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "printing"
    assert (row.wear_cost, row.wear_rate) == (None, None)
