"""A cost priced from Spoolman spools is not recomputed into a worse one
(upstream 39835437, #2591).

``POST /statistics/recalculate-costs`` rebuilds a cost from
``SpoolUsageHistory`` and falls back to the farm rate when there are no rows —
and in Spoolman mode there never are, so the fallback is a downgrade: it would
overwrite the figure completion priced from the linked spools, which cannot be
rebuilt from the archive row. Attaching a late 3MF re-priced the row the same
way, in either inventory mode.
"""

import zipfile

import pytest
from httpx import AsyncClient


async def _settings(db_session, **values):
    from sqlalchemy import delete

    from backend.app.models.settings import Settings

    await db_session.execute(delete(Settings).where(Settings.key.in_(list(values))))
    for key, value in values.items():
        db_session.add(Settings(key=key, value=value))
    await db_session.commit()


async def _archive(db_session, *, cost, grams=100.0, file_path="/tmp/a.3mf"):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=None,
        filename="a.3mf",
        file_path=file_path,
        file_size=1024,
        print_name="a",
        filament_used_grams=grams,
        cost=cost,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _cost(db_session, archive_id):
    from sqlalchemy import select

    from backend.app.models.archive import PrintArchive

    return (await db_session.execute(select(PrintArchive.cost).where(PrintArchive.id == archive_id))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recalculating_keeps_a_spoolman_priced_cost(async_client: AsyncClient, db_session):
    await _settings(db_session, spoolman_enabled="true", default_filament_cost="25")
    archive = await _archive(db_session, cost=4.0)

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text

    assert await _cost(db_session, archive.id) == pytest.approx(4.0)
    assert response.json()["preserved"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recalculating_still_prices_an_archive_that_has_no_cost(async_client: AsyncClient, db_session):
    await _settings(db_session, spoolman_enabled="true", default_filament_cost="25")
    archive = await _archive(db_session, cost=None)

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text

    assert await _cost(db_session, archive.id) == pytest.approx(2.5)
    assert response.json()["preserved"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_with_spoolman_off_recalculating_is_unchanged(async_client: AsyncClient, db_session):
    await _settings(db_session, spoolman_enabled="false", default_filament_cost="25")
    archive = await _archive(db_session, cost=4.0)

    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text

    assert await _cost(db_session, archive.id) == pytest.approx(2.5)


def _sliced_3mf(path, grams: float):
    body = f'<plate><metadata key="index" value="1" /><metadata key="weight" value="{grams}" /></plate>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", f"<config>{body}</config>")
    return path


async def _attach(db_session, tmp_path, monkeypatch, *, cost):
    from backend.app.core.config import settings as app_settings
    from backend.app.services.archive import ArchiveService

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    archive = await _archive(db_session, cost=cost, grams=None, file_path="")
    src = _sliced_3mf(tmp_path / "src.gcode.3mf", 100.0)
    assert await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, "a.gcode.3mf")
    return await _cost(db_session, archive.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_late_3mf_does_not_re_price_a_costed_archive(db_session, tmp_path, monkeypatch):
    await _settings(db_session, default_filament_cost="25")
    assert await _attach(db_session, tmp_path, monkeypatch, cost=4.0) == pytest.approx(4.0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_late_3mf_still_prices_an_archive_that_had_no_cost(db_session, tmp_path, monkeypatch):
    await _settings(db_session, default_filament_cost="25")
    assert await _attach(db_session, tmp_path, monkeypatch, cost=None) == pytest.approx(2.5)
