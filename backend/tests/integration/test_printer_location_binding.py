"""A location travels as an object and is written as an id."""

import pytest
from httpx import AsyncClient


async def _location(client: AsyncClient, name="Shop 2"):
    return (await client.post("/api/v1/printer-locations", json={"name": name})).json()


def _printer_body(**extra):
    body = {
        "name": "p1",
        "ip_address": "10.0.0.5",
        "serial_number": "S-1",
        "access_code": "12345678",
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_is_created_with_a_location_id(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer

    loc = await _location(async_client)
    db_session.add(
        Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1", location_id=loc["id"])
    )
    await db_session.commit()

    listed = (await async_client.get("/api/v1/printers/")).json()

    assert listed[0]["location"] == {"id": loc["id"], "name": "Shop 2", "parent_id": None, "path": "Shop 2"}
    assert listed[0]["location_id"] == loc["id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_without_a_location_reports_null(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer

    db_session.add(Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1"))
    await db_session.commit()

    listed = (await async_client.get("/api/v1/printers/")).json()

    assert listed[0]["location"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sending_the_old_location_key_is_refused_not_ignored(async_client: AsyncClient):
    """A client doing GET → edit → PUT sends the object back. Ignored, its
    location change would silently do nothing — which is the failure this whole
    stage exists to remove, reintroduced at the last step."""
    rsp = await async_client.post("/api/v1/printers/", json=_printer_body(location="Shop 2"))

    assert rsp.status_code == 422
    assert "location_id" in rsp.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_old_key_is_refused_on_update_too(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer

    db_session.add(Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1"))
    await db_session.commit()
    printer_id = (await async_client.get("/api/v1/printers/")).json()[0]["id"]

    rsp = await async_client.patch(f"/api/v1/printers/{printer_id}", json={"location": "Shop 2"})

    assert rsp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_location_that_does_not_exist_is_refused(async_client: AsyncClient, db_session):
    """SQLite does not enforce foreign keys, so without the guard the row would
    take a dangling id and the printer would show no place, with no error."""
    from backend.app.models.printer import Printer

    db_session.add(Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1"))
    await db_session.commit()
    printer_id = (await async_client.get("/api/v1/printers/")).json()[0]["id"]

    rsp = await async_client.patch(f"/api/v1/printers/{printer_id}", json={"location_id": 999})

    assert rsp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_can_be_moved_and_unset(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer

    loc = await _location(async_client)
    db_session.add(Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1"))
    await db_session.commit()
    printer_id = (await async_client.get("/api/v1/printers/")).json()[0]["id"]

    moved = await async_client.patch(f"/api/v1/printers/{printer_id}", json={"location_id": loc["id"]})
    assert moved.status_code == 200

    cleared = await async_client.patch(f"/api/v1/printers/{printer_id}", json={"location_id": None})
    assert cleared.status_code == 200
    assert (await async_client.get("/api/v1/printers/")).json()[0]["location"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_renaming_a_location_shows_through_on_the_printer(async_client: AsyncClient, db_session):
    """The capability the entity buys: one edit, everywhere."""
    from backend.app.models.printer import Printer

    loc = await _location(async_client)
    db_session.add(
        Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1", location_id=loc["id"])
    )
    await db_session.commit()

    await async_client.patch(f"/api/v1/printer-locations/{loc['id']}", json={"name": "Workshop"})

    listed = (await async_client.get("/api/v1/printers/")).json()
    assert listed[0]["location"]["name"] == "Workshop"
