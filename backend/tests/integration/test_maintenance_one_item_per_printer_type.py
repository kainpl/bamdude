"""One maintenance item per printer and type, whoever creates it (m244).

A printer's rows are created on first read, and "assign" checked before it
inserted: two readers, or a double click, each created a row. The unique index
now refuses the second insert; the creators take the first one's row instead of
failing, and "assign" answers the 400 it gives a late request.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from backend.app.api.routes import maintenance as routes
from backend.app.models.maintenance import MaintenanceType, PrinterMaintenance

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _custom_type(db_session, **kwargs) -> MaintenanceType:
    maint_type = MaintenanceType(name="Custom", default_interval_hours=100.0, is_system=False, **kwargs)
    db_session.add(maint_type)
    await db_session.commit()
    return maint_type


async def _count(db_session, printer_id: int, type_id: int) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(PrinterMaintenance)
        .where(PrinterMaintenance.printer_id == printer_id, PrinterMaintenance.maintenance_type_id == type_id)
    )


async def _another_reader_creates(db, printer_id: int, type_id: int, **settings) -> None:
    """The row a concurrent reader committed between this reader's check and its insert."""
    await db.execute(
        insert(PrinterMaintenance.__table__).values(
            printer_id=printer_id,
            maintenance_type_id=type_id,
            enabled=settings.get("enabled", True),
            last_performed_hours=settings.get("last_performed_hours", 0.0),
            custom_interval_hours=settings.get("custom_interval_hours"),
            custom_interval_type=settings.get("custom_interval_type"),
        )
    )


async def test_a_fresh_database_refuses_a_second_row_of_the_pair(db_session, printer_factory):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session)
    db_session.add(PrinterMaintenance(printer_id=printer.id, maintenance_type_id=maint_type.id))
    await db_session.commit()

    db_session.add(PrinterMaintenance(printer_id=printer.id, maintenance_type_id=maint_type.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_the_creator_takes_the_row_another_reader_created_and_keeps_its_own_work(db_session, printer_factory):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session)
    other_type = await _custom_type(db_session)

    # Work of the same request, flushed before the race: it must survive the refused insert.
    mine, created = await routes._get_or_create_maintenance_item(db_session, printer.id, other_type.id)
    assert created is True

    await _another_reader_creates(db_session, printer.id, maint_type.id, custom_interval_hours=7.0, enabled=False)
    item, created = await routes._get_or_create_maintenance_item(db_session, printer.id, maint_type.id)

    assert created is False
    assert (item.custom_interval_hours, item.enabled) == (7.0, False)
    await db_session.commit()
    assert await _count(db_session, printer.id, maint_type.id) == 1
    assert await _count(db_session, printer.id, other_type.id) == 1
    assert mine.id is not None


async def test_a_read_that_lost_the_race_answers_with_the_other_readers_rows(db_session, printer_factory, monkeypatch):
    printer = await printer_factory(model="X1C")
    real = routes._get_or_create_maintenance_item
    raced: list[int] = []

    async def lose_the_race(db, printer_id, type_id):
        if not raced:
            raced.append(type_id)
            await _another_reader_creates(
                db, printer_id, type_id, custom_interval_hours=3.0, custom_interval_type="days", enabled=False
            )
        return await real(db, printer_id, type_id)

    monkeypatch.setattr(routes, "_get_or_create_maintenance_item", lose_the_race)

    overview = await routes._get_printer_maintenance_internal(printer.id, db_session, commit=True)

    assert raced, "the printer had a system type without a row"
    status = next(s for s in overview.maintenance_items if s.maintenance_type_id == raced[0])
    # The other reader's row, as it is -- not the defaults this reader was about to insert.
    assert (status.interval_hours, status.interval_type, status.enabled) == (3.0, "days", False)
    assert status.id != 0
    assert all(s.id != 0 for s in overview.maintenance_items)
    assert await _count(db_session, printer.id, raced[0]) == 1
    # Every type of the printer has exactly one row.
    pairs = (
        await db_session.execute(
            select(PrinterMaintenance.maintenance_type_id, func.count())
            .where(PrinterMaintenance.printer_id == printer.id)
            .group_by(PrinterMaintenance.maintenance_type_id)
        )
    ).all()
    assert pairs and all(count == 1 for _type_id, count in pairs)


async def test_assign_twice_is_a_400(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session)
    url = f"/api/v1/maintenance/printers/{printer.id}/assign/{maint_type.id}"

    first = await async_client.post(url)
    assert first.status_code == 200
    second = await async_client.post(url)

    assert second.status_code == 400
    assert await _count(db_session, printer.id, maint_type.id) == 1


async def test_assign_that_lost_a_double_click_race_is_a_400_not_a_500(
    async_client: AsyncClient, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session)
    real = routes._get_or_create_maintenance_item

    async def lose_the_race(db, printer_id, type_id):
        # Both requests passed the "already assigned" check; the other one inserted first.
        await _another_reader_creates(db, printer_id, type_id)
        return await real(db, printer_id, type_id)

    monkeypatch.setattr(routes, "_get_or_create_maintenance_item", lose_the_race)

    response = await async_client.post(f"/api/v1/maintenance/printers/{printer.id}/assign/{maint_type.id}")

    assert response.status_code == 400
    assert "already assigned" in response.text or "вже" in response.text


async def test_perform_answers_with_the_items_own_interval_type(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session, interval_type="hours")
    assigned = await async_client.post(f"/api/v1/maintenance/printers/{printer.id}/assign/{maint_type.id}")
    item_id = assigned.json()["id"]
    patched = await async_client.patch(
        f"/api/v1/maintenance/items/{item_id}", json={"custom_interval_type": "days", "custom_interval_hours": 90.0}
    )
    assert patched.status_code == 200

    response = await async_client.post(f"/api/v1/maintenance/items/{item_id}/perform", json={})

    assert response.status_code == 200
    data = response.json()
    assert data["interval_type"] == "days"
    assert data["days_until_due"] == 90.0
    assert data["days_since_maintenance"] == 0
    assert data["hours_until_due"] == 0
    # ... and it is what the list says right after.
    overview = (await async_client.get(f"/api/v1/maintenance/printers/{printer.id}")).json()
    listed = next(s for s in overview["maintenance_items"] if s["id"] == item_id)
    assert listed["interval_type"] == "days"


async def test_perform_keeps_the_types_interval_type_without_an_override(
    async_client: AsyncClient, db_session, printer_factory
):
    printer = await printer_factory()
    maint_type = await _custom_type(db_session, interval_type="days")
    assigned = await async_client.post(f"/api/v1/maintenance/printers/{printer.id}/assign/{maint_type.id}")

    response = await async_client.post(f"/api/v1/maintenance/items/{assigned.json()['id']}/perform", json={})

    assert response.status_code == 200
    assert response.json()["interval_type"] == "days"
