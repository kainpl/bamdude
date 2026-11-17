"""A spool that is already low is announced when it is put into a slot or its
weight is corrected by hand — not only after the next print has drawn from it
(owner, 2026-11-17; upstream #2913 reaches the same case by polling)."""

from unittest.mock import AsyncMock

import pytest

from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.spool import SpoolUpdate
from backend.app.services import inventory_service, usage_tracker
from backend.app.services.notification_service import notification_service


@pytest.fixture
def announced(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(notification_service, "on_filament_low", mock)
    monkeypatch.setattr(usage_tracker, "_global_low_stock_threshold", AsyncMock(return_value=20.0))
    return mock


async def loaded_spool(db, *, used: float, in_slot: bool = True) -> Spool:
    printer = Printer(name="P1", serial_number="SN1", ip_address="192.0.2.1", access_code="0")
    spool = Spool(material="PLA", label_weight=1000, weight_used=used)
    db.add_all([printer, spool])
    await db.flush()
    if in_slot:
        db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=2))
    await db.commit()
    return spool


@pytest.mark.asyncio
async def test_a_low_spool_in_a_slot_is_announced(db_session, announced):
    spool = await loaded_spool(db_session, used=900)

    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)

    assert announced.await_count == 1
    assert announced.await_args.kwargs["remaining_percent"] == 10
    assert spool.low_stock_notified is True


@pytest.mark.asyncio
async def test_it_is_announced_once(db_session, announced):
    spool = await loaded_spool(db_session, used=900)

    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)
    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)

    assert announced.await_count == 1


@pytest.mark.asyncio
async def test_a_spool_with_plenty_left_is_quiet(db_session, announced):
    spool = await loaded_spool(db_session, used=100)

    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)

    assert announced.await_count == 0


@pytest.mark.asyncio
async def test_a_spool_on_the_shelf_is_not_this_announcements_business(db_session, announced):
    spool = await loaded_spool(db_session, used=900, in_slot=False)

    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)

    assert announced.await_count == 0
    assert not spool.low_stock_notified


@pytest.mark.asyncio
async def test_a_failed_announcement_does_not_reach_the_caller(db_session, announced):
    spool = await loaded_spool(db_session, used=900)
    announced.side_effect = RuntimeError("provider down")

    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)


@pytest.mark.asyncio
async def test_a_weight_corrected_by_hand_announces_a_loaded_spool(db_session, announced):
    spool = await loaded_spool(db_session, used=100)

    await inventory_service.update_spool(db_session, spool.id, SpoolUpdate(weight_used=950))

    assert announced.await_count == 1


@pytest.mark.asyncio
async def test_a_refill_by_hand_arms_the_next_run_down(db_session, announced):
    spool = await loaded_spool(db_session, used=950)
    await usage_tracker.warn_if_low_stock_in_slot(db_session, spool)
    await db_session.commit()

    await inventory_service.update_spool(db_session, spool.id, SpoolUpdate(weight_used=0))
    await inventory_service.update_spool(db_session, spool.id, SpoolUpdate(weight_used=950))

    assert announced.await_count == 2


@pytest.mark.asyncio
async def test_an_edit_of_another_field_asks_nothing(db_session, announced):
    spool = await loaded_spool(db_session, used=950)

    await inventory_service.update_spool(db_session, spool.id, SpoolUpdate(note="shelf B"))

    assert announced.await_count == 0


def test_every_assignment_writer_asks():
    import inspect

    from backend.app.api.routes import inventory
    from backend.app.services import auto_stock_spool, spool_tag_matcher

    for module in (inventory, auto_stock_spool, spool_tag_matcher):
        assert "warn_if_low_stock_in_slot" in inspect.getsource(module), module.__name__
