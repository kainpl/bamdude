"""One rule: a location in any response is {id, name} or null.

An exception would have to be explained at every site. This is the place that
would notice a new endpoint quietly inventing a different shape.
"""

import pytest
from httpx import AsyncClient


async def _place(db_session, name="Shop 2"):
    from backend.app.models.printer_location import PrinterLocation

    row = PrinterLocation(name=name, name_key=name.strip().lower())
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_reports_its_location_as_an_object(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer

    place = await _place(db_session)
    db_session.add(
        Printer(name="p1", ip_address="10.0.0.5", serial_number="U-1", access_code="1", location_id=place.id)
    )
    await db_session.commit()

    listed = (await async_client.get("/api/v1/printers/")).json()

    assert listed[0]["location"] == {"id": place.id, "name": "Shop 2", "parent_id": None, "path": "Shop 2"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_queue_reports_it_the_same_way(async_client: AsyncClient, db_session):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    place = await _place(db_session)
    printer = Printer(name="p1", ip_address="10.0.0.5", serial_number="U-2", access_code="1", location_id=place.id)
    db_session.add(printer)
    await db_session.commit()
    db_session.add(PrinterQueue(printer_id=printer.id))
    await db_session.commit()

    rsp = await async_client.get("/api/v1/queues/")
    assert rsp.status_code == 200, rsp.text
    body = rsp.json()
    queues = body["queues"] if isinstance(body, dict) and "queues" in body else body

    assert queues, body
    assert queues[0]["printer_location"] == {"id": place.id, "name": "Shop 2", "parent_id": None, "path": "Shop 2"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_no_place_reports_null_everywhere(async_client: AsyncClient, db_session):
    """Null, not an empty object and not an empty string — "nowhere recorded"
    has one representation."""
    from backend.app.models.printer import Printer

    db_session.add(Printer(name="p1", ip_address="10.0.0.5", serial_number="U-3", access_code="1"))
    await db_session.commit()

    assert (await async_client.get("/api/v1/printers/")).json()[0]["location"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_reports_its_location_as_an_object(async_client: AsyncClient, db_session):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    place = await _place(db_session)
    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    db_session.add(SmartSensor(name="Workshop", zigbee_ieee="aa:bb", location_id=place.id))
    await db_session.commit()

    # The radio is down in this test, and the sensor is listed anyway: the row,
    # its name and its place do not live in the radio. Which makes this the
    # right place to assert the shape of the place itself.
    listed = (await async_client.get("/api/v1/zigbee/sensors")).json()

    assert listed["sensors"][0]["location"] == {
        "id": place.id,
        "name": place.name,
        "parent_id": None,
        "path": place.name,
    }
    assert listed["sensors"][0]["present"] is False
