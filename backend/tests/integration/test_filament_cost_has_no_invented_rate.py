"""No filament rate is invented when the farm has not set one.

Six places carried a hardcoded 25.0 per kg for an unset
``default_filament_cost`` while ``usage_tracker`` used 0.0 — so the initial
archive estimate and the untracked-weight top-up disagreed on an empty setting,
and both reported money nobody had entered.

A rate of zero must not become a cost of 0.00 either: that is a number claiming
the print was free. No rate means no cost, which is the shape usage_tracker
already had.
"""

import pytest
from httpx import AsyncClient


@pytest.fixture
async def no_rate(db_session):
    """The farm has not set a filament rate."""
    from sqlalchemy import delete

    from backend.app.models.settings import Settings

    await db_session.execute(delete(Settings).where(Settings.key == "default_filament_cost"))
    await db_session.commit()


def test_the_setting_defaults_to_nothing():
    """An unset rate reported as 25 is where the invented number begins."""
    from backend.app.schemas.settings import AppSettings

    assert AppSettings.model_fields["default_filament_cost"].default == 0.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_rate_means_no_cost_on_a_new_archive(db_session, no_rate, tmp_path):
    """archive_print's initial estimate. It used 25.0 and wrote a number the
    operator never entered."""
    from backend.app.services.archive import ArchiveService

    service = ArchiveService(db_session)
    cost = await service._default_rate_cost(120.0)

    assert cost is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_real_rate_still_produces_a_cost(db_session, tmp_path):
    from backend.app.api.routes.settings import set_setting
    from backend.app.services.archive import ArchiveService

    await set_setting(db_session, "default_filament_cost", "20")
    await db_session.commit()

    service = ArchiveService(db_session)

    assert await service._default_rate_cost(500.0) == 10.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recalculating_costs_invents_nothing(async_client: AsyncClient, db_session, no_rate):
    """POST /archives/recalculate-costs walked every archive and stamped
    grams × 25 on any that had no spool history."""
    from sqlalchemy import select

    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=None,
        filename="a.3mf",
        file_path="/tmp/a.3mf",
        file_size=1024,
        print_name="a",
        filament_used_grams=100.0,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    response = await async_client.post("/api/v1/archives/recalculate-costs")
    assert response.status_code == 200, response.text

    cost = (await db_session.execute(select(PrintArchive.cost).where(PrintArchive.id == archive.id))).scalar_one()
    assert cost is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_project_plan_invents_nothing(async_client: AsyncClient, db_session, no_rate):
    """The plan estimates from file metadata alone — there is no spool to fall
    back to, so an invented rate was the only number it ever showed."""
    from backend.app.api.routes.projects import _get_default_filament_cost

    assert await _get_default_filament_cost(db_session) == 0.0
