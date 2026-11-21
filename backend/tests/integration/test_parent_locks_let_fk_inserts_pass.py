"""Our explicit lock of a parent never blocks a row that merely names it (G03 B, L6(b); acceptance TA32 F1 and F4).

A foreign-key check takes its parent ``FOR KEY SHARE``. ``FOR NO KEY UPDATE`` -- every explicit lock of a header, a
job row, an AutoQueue row and the order row ``policy_for(lock=True)`` takes -- does not conflict with it, so an insert
naming the held parent commits while it is held:

- F4: a Workshop journal insert referencing an order a claim holds (``policy_for(lock=True)``).
- F1: the runner's execution archive naming the queue header (``print_archives.queue_id``) while a Stop or a halt holds
  that header under its lane.

The former ``FOR UPDATE`` made each insert wait (negative controls). PostgreSQL only; SQLite serialises on its writer.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.services.printer_queues import ensure_printer_queue
from backend.tests.integration.lane_pairs import ROW_WAIT, postgres_edge
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def postgres_only(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL lock modes; SQLite serialises on its writer")


async def an_order(factory) -> int:
    async with factory() as db:
        order = Project(name="Held")
        db.add(order)
        await db.commit()
        return order.id


async def journal(factory, order_id) -> None:
    from backend.app.services import order_journal

    async with factory() as db:
        await order_journal.record(db, order_id, "fields_changed", {"fields": ["notes"]})
        await db.commit()


async def an_execution_archive(factory, queue_id) -> None:
    """The runner's insert of its execution archive, naming the queue header (F1)."""
    async with factory() as db:
        db.add(
            PrintArchive(
                printer_id=queue_id, queue_id=queue_id, filename="x.3mf", file_path="", file_size=0, status="printing"
            )
        )
        await db.commit()


async def waits(factory, task, holder_pid: int) -> None:
    """The insert's own backend waits on a row lock, and the edge leads to the holder's very backend."""
    edge = await postgres_edge(factory, task, on=ROW_WAIT, behind=holder_pid)
    assert holder_pid in edge.blockers
    assert not task.done()


async def test_a_journal_insert_naming_an_order_a_claim_holds_does_not_wait(postgres_only, test_engine):
    from backend.app.services.order_resources import policy_for

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await an_order(factory)
    async with factory() as claim:
        await policy_for(claim, order_id, lock=True)
        await asyncio.wait_for(journal(factory, order_id), 10)
        await claim.rollback()


async def test_the_former_for_update_of_the_order_made_that_insert_wait(postgres_only, test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await an_order(factory)
    async with factory() as holder:
        await holder.execute(select(Project.id).where(Project.id == order_id).with_for_update())
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        inserting = asyncio.create_task(journal(factory, order_id))
        try:
            await waits(factory, inserting, holder_pid)
        finally:
            await holder.rollback()
        await asyncio.wait_for(inserting, 10)


async def test_an_execution_archive_naming_a_header_a_stop_holds_does_not_wait(
    postgres_only, test_engine, db_session, printer_factory
):
    from backend.app.services.queue_ops import claim_lanes

    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as stop, claim_lanes(stop, [printer.id]):
        # The Stop's (and halt_lane's) header lock: FOR NO KEY UPDATE, under the lane.
        await stop.scalar(select(PrinterQueue.id).where(PrinterQueue.id == printer.id).with_for_update(key_share=True))
        await asyncio.wait_for(an_execution_archive(factory, printer.id), 10)
        await stop.rollback()


async def test_the_former_for_update_of_the_header_made_that_insert_wait(
    postgres_only, test_engine, db_session, printer_factory
):
    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as holder:
        await holder.execute(select(PrinterQueue.id).where(PrinterQueue.id == printer.id).with_for_update())
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        inserting = asyncio.create_task(an_execution_archive(factory, printer.id))
        try:
            await waits(factory, inserting, holder_pid)
        finally:
            await holder.rollback()
        await asyncio.wait_for(inserting, 10)
