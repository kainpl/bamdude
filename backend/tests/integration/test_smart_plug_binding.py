"""Rebinding a plug, and what it does to a measurement already in flight.

Moving a plug mid-print makes the archive's delta meaningless: `energy_start_kwh`
was captured against a different physical meter, so the end-handler would
subtract two unrelated counters and record a **wrong** number rather than a
missing one.

Refusing the move would put an accounting side-effect ahead of an operator's
decision on their own farm, so the move is allowed and the measurement is voided
instead. Nothing downstream needs new code: the end-handler already returns early
and records nothing when the start value is NULL.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


async def _in_flight(archive_factory, printer_id: int, start_kwh: float):
    """A print that started measuring and has not finished.

    Identified downstream by ``completed_at IS NULL`` plus a non-NULL
    ``energy_start_kwh``, which together need no assumption about the ``status``
    vocabulary — but the row is given a truthful status anyway.
    """
    return await archive_factory(printer_id, status="printing", completed_at=None, energy_start_kwh=start_kwh)


class TestVoidingTheInflightMeasurement:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_moving_to_another_printer_clears_the_start_reading(
        self, async_client: AsyncClient, db_session, archive_factory, smart_plug_factory, printer_factory
    ):
        old = await printer_factory()
        new = await printer_factory()
        plug = await smart_plug_factory(printer_id=old.id, controls_printer_power=True)
        archive = await _in_flight(archive_factory, old.id, 12.5)

        resp = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": new.id})

        assert resp.status_code == 200
        await db_session.refresh(archive)
        assert archive.energy_start_kwh is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unlinking_clears_it_too(
        self, async_client: AsyncClient, db_session, archive_factory, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        plug = await smart_plug_factory(printer_id=printer.id, controls_printer_power=True)
        archive = await _in_flight(archive_factory, printer.id, 3.0)

        resp = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": None})

        assert resp.status_code == 200
        await db_session.refresh(archive)
        assert archive.energy_start_kwh is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_finished_archive_is_left_alone(
        self, async_client: AsyncClient, db_session, archive_factory, smart_plug_factory, printer_factory
    ):
        """Only an in-flight measurement is at risk. A completed archive holds a
        figure computed against the meter that took it."""
        printer = await printer_factory()
        plug = await smart_plug_factory(printer_id=printer.id, controls_printer_power=True)
        done = await archive_factory(printer.id, energy_start_kwh=7.0, completed_at=datetime.now(timezone.utc))

        await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": None})

        await db_session.refresh(done)
        assert done.energy_start_kwh == 7.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_accessory_plug_voids_nothing(
        self, async_client: AsyncClient, db_session, archive_factory, smart_plug_factory, printer_factory
    ):
        """An accessory was never the meter behind the measurement, so moving it
        changes nothing about the figure being taken."""
        printer = await printer_factory()
        plug = await smart_plug_factory(printer_id=printer.id, controls_printer_power=False)
        archive = await _in_flight(archive_factory, printer.id, 4.5)

        await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": None})

        await db_session.refresh(archive)
        assert archive.energy_start_kwh == 4.5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_unrelated_edit_voids_nothing(
        self, async_client: AsyncClient, db_session, archive_factory, smart_plug_factory, printer_factory
    ):
        """Renaming a plug is not rebinding it. Clearing the measurement on every
        save would make the figure disappear for reasons nobody could trace."""
        printer = await printer_factory()
        plug = await smart_plug_factory(printer_id=printer.id, controls_printer_power=True)
        archive = await _in_flight(archive_factory, printer.id, 9.0)

        await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"name": "renamed"})

        await db_session.refresh(archive)
        assert archive.energy_start_kwh == 9.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_running_archive_is_not_an_error(
        self, async_client: AsyncClient, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        plug = await smart_plug_factory(printer_id=printer.id, controls_printer_power=True)

        resp = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": None})

        assert resp.status_code == 200
