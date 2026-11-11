"""Publication and cancellation of one entry are serialised by the operation's rows (spec r2 §8.3 step 9, §8.4).

On a disposable embedded PostgreSQL, with a barrier holding the publication inside its transaction after the claim
took the submission and entry rows:
- a cancel that arrives then waits for that transaction; the publication commits first and its entry stays
  published -- the cancel fences only what is left;
- a cancel that commits first makes the publication refuse under the same rows: nothing is queued.
"""

import asyncio

import pytest
from sqlalchemy import event, func, select

from backend.app.core.database import _strip_tz_from_params
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import connector_submissions as cs
from backend.tests.integration.test_connector_submissions import (  # noqa: F401
    _body,
    _clean_spool_state,
    _count,
    _printer_entry,
    farm,
    sessions,
)
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)


async def test_a_cancel_waits_for_a_publication_already_holding_the_rows(farm, sessions, monkeypatch):  # noqa: F811
    inside, release = asyncio.Event(), asyncio.Event()
    real_record = cs.ConnectorReceipt.record

    async def held_record(self, session, **ids):
        await real_record(self, session, **ids)
        inside.set()
        await release.wait()

    monkeypatch.setattr(cs.ConnectorReceipt, "record", held_record)
    first, second = _printer_entry(farm, quantity=1), _printer_entry(farm, quantity=1)
    body = _body(farm, first, second)
    registered = await cs.register(farm["actor"], body)

    publication = asyncio.create_task(cs.run(registered.submission_id, farm["actor"]))
    await asyncio.wait_for(inside.wait(), 30)
    cancel = asyncio.create_task(cs.cancel(registered.proof))
    await asyncio.sleep(1.0)
    assert not cancel.done(), "the cancel must wait for the publication's rows"

    release.set()
    await asyncio.wait_for(cancel, 30)
    await asyncio.wait_for(publication, 30)

    result = await cs.result(farm["actor"], body.operation_id)
    assert [e.state for e in result.entries] == ["published", "cancelled"]
    assert await _count(sessions, PrintQueueItem) == 1


async def test_a_cancel_that_committed_first_stops_the_publication(farm, sessions, monkeypatch):  # noqa: F811
    real_claim = cs.ConnectorReceipt.claim
    claimed = asyncio.Event()
    cancelled = asyncio.Event()

    async def claim_after_cancel(self, session, source):
        claimed.set()
        await cancelled.wait()
        await real_claim(self, session, source)

    monkeypatch.setattr(cs.ConnectorReceipt, "claim", claim_after_cancel)
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)

    publication = asyncio.create_task(cs.run(registered.submission_id, farm["actor"]))
    await asyncio.wait_for(claimed.wait(), 30)
    await cs.cancel(registered.proof)
    cancelled.set()
    await asyncio.wait_for(publication, 30)

    result = await cs.result(farm["actor"], body.operation_id)
    assert result.state == "cancelled"
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PrintQueueItem)) == 0
