"""A sensor belongs to a place or to a printer — the operator picks which.

An enclosure probe or a door contact belongs to one machine; a thermometer
measuring the workshop belongs to the room. Both are real answers, so the
binding is a choice rather than something inferred from the hardware.

⚠️ **The two are exclusive**, and exclusive by construction rather than by a
check somebody can forget: setting either side clears the other. They answer the
same question — where this reading belongs — and a printer already has a
location, so a sensor holding both could claim a place its printer is not in and
appear in two lists at once.

(Upstream solves the same need with a separate Home-Assistant-only table bound
to the printer. We own our sensors over Zigbee directly, so the binding is a
property of the sensor we already have rather than a reason for a second kind.)
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.api.routes.zigbee import _bind_sensor
from backend.app.models.printer import Printer
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.smart_sensor import SmartSensor
from backend.app.services.printer_location_service import location_key


async def _place(db_session, name: str = "Workshop") -> PrinterLocation:
    place = PrinterLocation(name=name, name_key=location_key(name))
    db_session.add(place)
    await db_session.commit()
    await db_session.refresh(place)
    return place


async def _printer(db_session, name: str = "X1C") -> Printer:
    printer = Printer(name=name, ip_address="192.168.1.50", access_code="12345678", serial_number=f"SN{name}")
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


async def _sensor(db_session, **fields) -> SmartSensor:
    sensor = SmartSensor(name="probe", zigbee_ieee=f"00:11:22:33:44:55:66:{len(fields)}7", **fields)
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)
    return sensor


@pytest.mark.asyncio
@pytest.mark.integration
class TestChoosingABinding:
    async def test_a_sensor_can_be_bound_to_a_place(self, db_session):
        place = await _place(db_session)
        sensor = await _sensor(db_session)

        await _bind_sensor(
            db_session, sensor, location_id=place.id, printer_id=None, set_location=True, set_printer=True
        )

        assert sensor.location_id == place.id
        assert sensor.printer_id is None

    async def test_a_sensor_can_be_bound_to_a_printer(self, db_session):
        printer = await _printer(db_session)
        sensor = await _sensor(db_session)

        await _bind_sensor(
            db_session, sensor, location_id=None, printer_id=printer.id, set_location=True, set_printer=True
        )

        assert sensor.printer_id == printer.id
        assert sensor.location_id is None

    async def test_neither_is_a_valid_answer(self, db_session):
        """An adopted sensor nobody has placed yet is not an error."""
        sensor = await _sensor(db_session)

        await _bind_sensor(db_session, sensor, location_id=None, printer_id=None, set_location=True, set_printer=True)

        assert sensor.location_id is None
        assert sensor.printer_id is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheyAreExclusive:
    async def test_binding_to_a_printer_clears_the_place(self, db_session):
        place = await _place(db_session)
        printer = await _printer(db_session)
        sensor = await _sensor(db_session, location_id=place.id)

        await _bind_sensor(
            db_session, sensor, location_id=None, printer_id=printer.id, set_location=False, set_printer=True
        )

        assert sensor.printer_id == printer.id
        assert sensor.location_id is None, "a sensor cannot claim a place its printer may not be in"

    async def test_binding_to_a_place_clears_the_printer(self, db_session):
        place = await _place(db_session)
        printer = await _printer(db_session)
        sensor = await _sensor(db_session, printer_id=printer.id)

        await _bind_sensor(
            db_session, sensor, location_id=place.id, printer_id=None, set_location=True, set_printer=False
        )

        assert sensor.location_id == place.id
        assert sensor.printer_id is None

    async def test_sending_both_prefers_the_printer_and_still_clears_the_place(self, db_session):
        """⚠️ A client that sends both gets ONE binding, never two."""
        place = await _place(db_session)
        printer = await _printer(db_session)
        sensor = await _sensor(db_session)

        await _bind_sensor(
            db_session, sensor, location_id=place.id, printer_id=printer.id, set_location=True, set_printer=True
        )

        assert sensor.printer_id == printer.id
        assert sensor.location_id is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestWhatAnUpdateLeavesAlone:
    async def test_an_update_that_mentions_neither_keeps_the_binding(self, db_session):
        """Renaming a sensor must not unbind it."""
        printer = await _printer(db_session)
        sensor = await _sensor(db_session, printer_id=printer.id)

        await _bind_sensor(db_session, sensor, location_id=None, printer_id=None, set_location=False, set_printer=False)

        assert sensor.printer_id == printer.id

    async def test_an_explicit_null_unbinds(self, db_session):
        """⚠️ The distinction the routes' ``model_fields_set`` check exists for:
        without it a sensor once given a place could never become placeless."""
        place = await _place(db_session)
        sensor = await _sensor(db_session, location_id=place.id)

        await _bind_sensor(db_session, sensor, location_id=None, printer_id=None, set_location=True, set_printer=False)

        assert sensor.location_id is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestRefusingWhatDoesNotExist:
    async def test_an_unknown_printer(self, db_session):
        from fastapi import HTTPException

        sensor = await _sensor(db_session)

        with pytest.raises(HTTPException) as caught:
            await _bind_sensor(
                db_session, sensor, location_id=None, printer_id=99999, set_location=False, set_printer=True
            )
        assert caught.value.status_code == 422

    async def test_an_unknown_place(self, db_session):
        from fastapi import HTTPException

        sensor = await _sensor(db_session)

        with pytest.raises(HTTPException) as caught:
            await _bind_sensor(
                db_session, sensor, location_id=99999, printer_id=None, set_location=True, set_printer=False
            )
        assert caught.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_the_printer_unbinds_rather_than_deleting_the_sensor(db_session):
    """⚠️ A sensor is physical hardware that outlives the printer it was taped
    to. Deleting the printer must not delete an adopted device — and refusing to
    delete a printer because a thermometer points at it would be worse."""
    printer = await _printer(db_session)
    sensor = await _sensor(db_session, printer_id=printer.id)
    sensor_id = sensor.id

    await db_session.delete(printer)
    await db_session.commit()

    remaining = (await db_session.execute(select(SmartSensor).where(SmartSensor.id == sensor_id))).scalar_one_or_none()
    assert remaining is not None, "the sensor must survive its printer"
    assert remaining.printer_id is None, "and be left unbound rather than pointing at a row that is gone"
