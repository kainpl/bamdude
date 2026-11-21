"""Closing a noncurrent execution locks only its own printer's claims (G03 B, L1; re-review B1-02).

``publish_start`` holds printer A's lane and closes the earlier execution's claims (``close_noncurrent``). A direct
Reprint on printer B names that same archive as its source until its own execution archive is written, so B's queue
can hold a ``printing`` row with this ``archive_id``. B's transaction holds that row. The close must not ask for it --
B's lane is not held -- yet still refuse (``foreign claim``) while B holds it. The former statement locked every
``printing`` row of the archive and waited behind B. PostgreSQL only: on SQLite B's lane holds the writer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_execution import ExecutionChanged, decide_start, publish_start, recheck_start
from backend.app.services.print_observation import PrintObservation
from backend.app.services.queue_ops import claim_lanes
from backend.tests.integration.lane_pairs import bypass_the_process_lane
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_duplicate_print_execution import setup

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_the_close_refuses_a_foreign_claim_without_waiting_for_its_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks; SQLite serialises lane holders on its writer")
    printer, queue, original, _item = await setup(db_session, printer_factory, archive_factory)
    printer_id, queue_id, archive_id = printer.id, queue.id, original.id
    decision = await decide_start(
        db_session,
        printer_id,
        {
            "subtask_id": "200",
            "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 2),
        },
    )
    await recheck_start(db_session, printer_id, decision, still_current=lambda: True)
    other_id = (await printer_factory()).id
    db_session.add(PrinterQueue(id=other_id, printer_id=other_id, status="printing"))
    await db_session.flush()
    claim = PrintQueueItem(
        queue_id=other_id,
        archive_id=archive_id,
        status="printing",
        origin="direct",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(claim)
    await db_session.commit()
    claim_id = claim.id
    for lane in (queue_id, other_id):
        bypass_the_process_lane(test_engine, monkeypatch, lane)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def publish():
        async with sessions() as db:
            current = PrintArchive(
                printer_id=printer_id,
                queue_id=queue_id,
                filename="observed.3mf",
                file_path="",
                file_size=0,
                subtask_id="200",
                status="printing",
                extra_data={},
            )
            try:
                await publish_start(db, current, decision)
            except ExecutionChanged as exc:
                return str(exc)
            return "published"

    async with sessions() as holder, claim_lanes(holder, [other_id]):
        await holder.scalar(select(PrintQueueItem.id).where(PrintQueueItem.id == claim_id).with_for_update())
        try:
            # A wait behind B would last until the rollback below.
            outcome = await asyncio.wait_for(publish(), 10)
        finally:
            await holder.rollback()
    assert outcome == "foreign claim"
