"""GET /statistics/aggregate over HTTP.

The service-level behaviour is pinned in tests/unit/services; what matters here
is the contract the frontend reads, the timezone header, and that an empty range
is an answer rather than a 404.
"""

from datetime import datetime

import pytest

from backend.app.models.archive import PrintArchive


async def _seed(db_session, **values) -> None:
    defaults = {
        "filename": "part.3mf",
        "file_path": "",
        "file_size": 0,
        "status": "completed",
        "quantity": 1,
        "created_at": datetime(2026, 9, 7, 22, 30),  # 8 Sep in Kyiv
    }
    db_session.add(PrintArchive(**{**defaults, **values}))
    await db_session.commit()


@pytest.mark.asyncio
async def test_the_aggregate_answers_the_shape_the_frontend_expects(async_client, db_session):
    await _seed(db_session, filament_type="PLA", filament_color="#FF0000", filament_used_grams=42.0)

    response = await async_client.get("/api/v1/statistics/aggregate", headers={"X-Client-Timezone": "Europe/Kyiv"})
    assert response.status_code == 200
    body = response.json()

    assert body["timezone"] == "Europe/Kyiv"
    assert body["granularity"] == "day"
    assert len(body["by_hour_of_day"]) == 24
    for key in ("buckets", "by_printer", "by_material", "by_color", "by_duration", "totals", "records"):
        assert key in body

    assert body["buckets"][0]["at"] == "2026-09-08"  # folded into the caller's zone
    assert body["totals"]["prints"] == 1
    assert body["by_material"][0]["material"] == "PLA"
    assert body["by_color"][0]["color"] == "#FF0000"


@pytest.mark.asyncio
async def test_a_range_of_a_week_or_less_answers_hourly(async_client):
    week = await async_client.get("/api/v1/statistics/aggregate?date_from=2026-09-01&date_to=2026-09-07")
    assert week.json()["granularity"] == "hour"

    longer = await async_client.get("/api/v1/statistics/aggregate?date_from=2026-09-01&date_to=2026-09-08")
    assert longer.json()["granularity"] == "day"


@pytest.mark.asyncio
async def test_an_empty_range_is_zeros_not_a_404(async_client):
    response = await async_client.get("/api/v1/statistics/aggregate?date_from=1999-01-01&date_to=1999-01-02")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["prints"] == 0
    assert body["buckets"] == []
    assert body["records"]["longest"] is None
    assert body["records"]["success_streak"] == 0


@pytest.mark.asyncio
async def test_without_the_header_it_falls_back_to_the_server_zone(async_client, db_session):
    await _seed(db_session)
    response = await async_client.get("/api/v1/statistics/aggregate")
    assert response.status_code == 200
    assert response.json()["timezone"]  # whatever TZ resolves to, never empty


@pytest.mark.asyncio
async def test_it_needs_an_archive_read_permission(async_client):
    """The route sits behind the same ownership pair as the rest of /archives."""
    response = await async_client.get("/api/v1/statistics/aggregate", headers={"Authorization": "Bearer nope"})
    assert response.status_code in (401, 403)
