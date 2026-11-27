"""What the AutoQueue printer order reads (C01, consilium r1 R1-03 / R1-04)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.printer_pick_order import read_pick_order
from backend.app.utils.failure_reasons import DISPATCH_UPLOAD_FAILED

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


async def _order(db_session, value):
    db_session.add(Settings(key="model_dispatch_order", value=value))
    await db_session.commit()


async def test_the_population_is_every_active_unarchived_printer(db_session, printer_factory):
    online = await printer_factory(name="online")
    paused = await printer_factory(name="paused")
    db_session.add(PrinterQueue(id=paused.id, printer_id=paused.id, is_paused=True))
    inactive = await printer_factory(name="inactive", is_active=False)
    archived = await printer_factory(name="archived", archived=True)
    await db_session.commit()

    order = await read_pick_order(db_session, now=NOW)

    assert {online.id, paused.id} <= set(order.facts)
    assert inactive.id not in order.facts
    assert archived.id not in order.facts


async def test_hours_are_runtime_plus_offset_and_the_priority_is_read(db_session, printer_factory):
    printer = await printer_factory(runtime_seconds=7200, print_hours_offset=1.5, dispatch_priority=4)

    facts = (await read_pick_order(db_session, now=NOW)).facts[printer.id]

    assert facts.hours == pytest.approx(3.5)
    assert facts.priority == 4


async def test_an_unknown_stored_order_reads_as_the_default(db_session, printer_factory):
    await printer_factory()
    assert (await read_pick_order(db_session, now=NOW)).order == "fewest_hours"
    await _order(db_session, "newest")
    assert (await read_pick_order(db_session, now=NOW)).order == "fewest_hours"


async def test_a_known_stored_order_is_read(db_session, printer_factory):
    await printer_factory()
    await _order(db_session, "priority")
    assert (await read_pick_order(db_session, now=NOW)).order == "priority"


async def test_lru_counts_only_real_finishes(db_session, printer_factory, archive_factory):
    await _order(db_session, "least_recently_used")
    printer = await printer_factory()
    never = await printer_factory()
    trashed_only = await printer_factory()
    # The real finish, 10 minutes ago (naive UTC, as every writer stores it).
    await archive_factory(printer.id, status="completed", completed_at=_naive(NOW - timedelta(minutes=10)))
    # Later attempts that printed nothing are not finishes.
    await archive_factory(
        printer.id,
        status="failed",
        failure_reason=DISPATCH_UPLOAD_FAILED,
        completed_at=_naive(NOW - timedelta(minutes=2)),
    )
    await archive_factory(
        printer.id,
        status="cancelled",
        extra_data={"dispatch_aborted": True},
        completed_at=_naive(NOW - timedelta(minutes=1)),
    )
    await archive_factory(printer.id, status="archived", completed_at=_naive(NOW - timedelta(seconds=30)))
    # A finished print in the trash still is the printer's last finish.
    await archive_factory(
        trashed_only.id,
        status="completed",
        completed_at=_naive(NOW - timedelta(hours=2)),
        deleted_at=_naive(NOW - timedelta(hours=1)),
    )

    facts = (await read_pick_order(db_session, now=NOW)).facts

    assert facts[printer.id].last_finished == pytest.approx(-600.0)
    assert facts[never.id].last_finished is None
    assert facts[trashed_only.id].last_finished == pytest.approx(-7200.0)


async def test_lru_is_not_read_under_another_order(db_session, printer_factory, archive_factory):
    printer = await printer_factory()
    await archive_factory(printer.id, status="completed", completed_at=_naive(NOW - timedelta(minutes=10)))
    assert (await read_pick_order(db_session, now=NOW)).facts[printer.id].last_finished is None
