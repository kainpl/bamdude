"""The filament a print used can be typed into its archive (audit D6 part 2, upstream d227d422).

An archive whose 3MF never arrived carries no weight, and nothing could supply
one afterwards — a rescan needs the file. Edit Archive takes the figure by hand.
It is the ARCHIVE's figure only: statistics, cost and order metrics read it,
and no spool is debited — the archive is the print history, the spools are the
inventory, and a typed estimate must not move real stock.

We have no per-run log table (upstream mirrors the grams into PrintLogEntry
because their aggregates sum that); every aggregate here reads the archive's
own column, so the edit reaches them as it is.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select


async def _patch(client: AsyncClient, archive_id: int, body: dict):
    return await client.patch(f"/api/v1/archives/{archive_id}", json=body)


async def _set_rate(db_session, per_kg: str) -> None:
    from backend.app.api.routes.settings import set_setting

    await set_setting(db_session, "default_filament_cost", per_kg)
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_without_a_figure_can_be_given_one(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="", filament_used_grams=None)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 46.16})

    assert response.status_code == 200
    assert response.json()["filament_used_grams"] == pytest.approx(46.16)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_figure_can_be_cleared(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, filament_used_grams=12.0)

    response = await _patch(async_client, archive.id, {"filament_used_grams": None})

    assert response.status_code == 200
    assert response.json()["filament_used_grams"] is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("grams", [-1, 100_000.5])
async def test_the_figure_is_bounded(async_client, archive_factory, printer_factory, grams):
    """It feeds the totals: a negative would subtract, 100 kg is past any print."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id)

    response = await _patch(async_client, archive.id, {"filament_used_grams": grams})

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_empty_cost_follows_the_typed_figure(async_client, archive_factory, printer_factory, db_session):
    await _set_rate(db_session, "20")
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="", filament_used_grams=None, cost=None)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 500})

    assert response.json()["cost"] == pytest.approx(10.0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_cost_derived_from_the_old_figure_follows_the_new_one(
    async_client, archive_factory, printer_factory, db_session
):
    await _set_rate(db_session, "20")
    printer = await printer_factory()
    # 50 g at 20/kg — exactly what the farm rate made of the old figure.
    archive = await archive_factory(printer.id, filament_used_grams=50.0, cost=1.0)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 250})

    assert response.json()["cost"] == pytest.approx(5.0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_cost_from_the_spools_is_left_alone(async_client, archive_factory, printer_factory, db_session):
    """Spool tracking prices each spool at its own rate; a typed weight at the
    farm's rate would overwrite the better number."""
    await _set_rate(db_session, "20")
    printer = await printer_factory()
    archive = await archive_factory(printer.id, filament_used_grams=50.0, cost=3.47)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 250})

    assert response.json()["cost"] == pytest.approx(3.47)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_cost_sent_with_the_figure_wins(async_client, archive_factory, printer_factory, db_session):
    await _set_rate(db_session, "20")
    printer = await printer_factory()
    archive = await archive_factory(printer.id, filament_used_grams=None, cost=None)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 500, "cost": 7.5})

    assert response.json()["cost"] == pytest.approx(7.5)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_rate_leaves_the_cost_unknown(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="", filament_used_grams=None, cost=None)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 500})

    assert response.json()["cost"] is None, "no rate is not a free print"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_spool_is_debited(async_client, archive_factory, printer_factory, db_session):
    from backend.app.models.spool import Spool
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    printer = await printer_factory()
    spool = Spool(material="PLA", label_weight=1000, weight_used=100.0)
    db_session.add(spool)
    await db_session.commit()
    archive = await archive_factory(printer.id, file_path="", filament_used_grams=None)

    response = await _patch(async_client, archive.id, {"filament_used_grams": 46.16})

    assert response.status_code == 200
    await db_session.refresh(spool)
    assert spool.weight_used == pytest.approx(100.0)
    assert (await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory))) == 0
