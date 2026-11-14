"""Archive-on-runout switch: off unless somebody turned it on, and what a PUT
stores is what the zero-point reads."""

import pytest

from backend.app.services.usage_tracker import _archive_on_runout_enabled

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_runout_archive_defaults_off_and_round_trips(async_client, db_session):
    before = await async_client.get("/api/v1/settings/")
    assert before.status_code == 200, before.text
    assert before.json()["runout_archive_spool_enabled"] is False
    assert await _archive_on_runout_enabled(db_session) is False  # no row at all

    put = await async_client.put("/api/v1/settings/", json={"runout_archive_spool_enabled": True})
    assert put.status_code == 200, put.text
    assert put.json()["runout_archive_spool_enabled"] is True

    after = await async_client.get("/api/v1/settings/")
    assert after.json()["runout_archive_spool_enabled"] is True
    assert await _archive_on_runout_enabled(db_session) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_before_null", [False, True])
async def test_an_explicit_null_neither_breaks_the_read_nor_contradicts_the_services(
    async_client, db_session, changed_before_null
):
    """A PUT with ``null`` stores the string "None". For a bool field outside the
    explicit list that made GET /settings a 500; and each answer must match what
    the service reading the row concludes — archive: off unless "true";
    zero-point and bidirectional sync: on unless "false"."""
    if changed_before_null:
        previous = await async_client.put(
            "/api/v1/settings/",
            json={
                "runout_archive_spool_enabled": True,
                "runout_zero_point_enabled": False,
                "ams_sync_bidirectional": False,
            },
        )
        assert previous.status_code == 200, previous.text
    put = await async_client.put(
        "/api/v1/settings/",
        json={"runout_archive_spool_enabled": None, "runout_zero_point_enabled": None, "ams_sync_bidirectional": None},
    )
    assert put.status_code == 200, put.text

    got = await async_client.get("/api/v1/settings/")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["runout_archive_spool_enabled"] is False
    assert body["runout_zero_point_enabled"] is True
    assert body["ams_sync_bidirectional"] is True
    assert await _archive_on_runout_enabled(db_session) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["mqtt_password", "mqtt_use_tls", "ha_enabled"])
async def test_nullable_runout_toggles_do_not_make_other_null_settings_valid(async_client, field):
    initial = await async_client.put("/api/v1/settings/", json={"runout_archive_spool_enabled": True})
    assert initial.status_code == 200, initial.text
    refused = await async_client.put("/api/v1/settings/", json={"runout_archive_spool_enabled": None, field: None})
    assert refused.status_code == 422, refused.text
    current = await async_client.get("/api/v1/settings/")
    assert current.status_code == 200 and current.json()["runout_archive_spool_enabled"] is True
