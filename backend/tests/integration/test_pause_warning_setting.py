"""``pause_warning_minutes``: one global lead time, 0–120, default 5; 0 = the event never fires."""

import pytest
from httpx import AsyncClient

from backend.app.models.settings import Settings
from backend.app.services import pause_warning


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_setting_round_trips_as_an_integer_and_refuses_values_outside_its_range(async_client: AsyncClient):
    assert (await async_client.get("/api/v1/settings/")).json()["pause_warning_minutes"] == 5

    saved = await async_client.put("/api/v1/settings/", json={"pause_warning_minutes": 15})
    assert saved.status_code == 200
    assert (await async_client.get("/api/v1/settings/")).json()["pause_warning_minutes"] == 15

    assert (await async_client.put("/api/v1/settings/", json={"pause_warning_minutes": 0})).status_code == 200
    assert (await async_client.get("/api/v1/settings/")).json()["pause_warning_minutes"] == 0

    for bad in (-1, 121):
        assert (await async_client.put("/api/v1/settings/", json={"pause_warning_minutes": bad})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("stored", "expected"), [(None, 5), ("12", 12), ("0", 0), ("garbage", 5), ("999", 120), ("-3", 0)]
)
async def test_the_reader_falls_back_to_the_default_and_clamps(db_session, stored, expected):
    if stored is not None:
        db_session.add(Settings(key="pause_warning_minutes", value=stored))
        await db_session.commit()
    assert await pause_warning.warning_minutes(db_session) == expected


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_provider_form_is_offered_the_event_off_by_default(async_client: AsyncClient):
    rows = (await async_client.get("/api/v1/notifications/events")).json()
    row = next(row for row in rows if row["flag"] == "on_pause_coming_up")
    assert (row["event_types"], row["group"], row["severity"], row["default"]) == (
        ["pause_coming_up"],
        "print",
        "info",
        False,
    )
