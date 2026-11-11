"""A receipt threaded through either queue writer commits with the jobs or not at all (OrcaBamDude spec r2 §8.3, A1).

The writer calls ``claim`` under its guards before any job row and ``record`` after the rows, in the publication's
own transaction:
- **Refused by ``AlreadyPublished``.** Nothing is committed: no job, no counter, no new source row.
- **A raise inside ``record``.** The jobs go with it.
- **No receipt.** The writer is unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import QueueSource
from backend.app.models.settings import Settings
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.queue_add import add_items_to_printer_queue
from backend.app.services.queue_publication import AlreadyPublished
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _clean_spool_state():
    queue_sources._reset_state()
    yield
    queue_sources._reset_state()


@pytest.fixture
def spool_sessions(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


class Receipt:
    """Writes one marker row inside the publication's transaction -- the stand-in for a connector receipt."""

    def __init__(self, *, refuse: BaseException | None = None, fail_record: BaseException | None = None):
        self.refuse, self.fail_record = refuse, fail_record
        self.claimed = 0
        self.recorded: tuple[list[int], list[int]] | None = None

    async def claim(self, session, source):
        self.claimed += 1
        self.claimed_sha = source.sha256
        if self.refuse is not None:
            raise self.refuse

    async def record(self, session, *, queue_item_ids, auto_item_ids):
        self.recorded = (list(queue_item_ids), list(auto_item_ids))
        session.add(Settings(key="receipt-marker", value=",".join(map(str, queue_item_ids + auto_item_ids))))
        await session.flush()
        if self.fail_record is not None:
            raise self.fail_record


async def _count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _marker(sessions) -> str | None:
    async with sessions() as session:
        return await session.scalar(select(Settings.value).where(Settings.key == "receipt-marker"))


async def _printer_add(db, source, queue, receipt, quantity=2):
    return await add_items_to_printer_queue(
        db,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15, quantity=quantity),
        None,
        publication_receipt=receipt,
    )


async def _auto_add(db, source, receipt, quantity=2):
    return await add_items_to_auto_queue(
        db,
        AutoQueueItemCreate(library_file_id=source.id, plate_id=15, quantity=quantity),
        None,
        publication_receipt=receipt,
    )


async def test_the_printer_writer_records_the_exact_job_ids_in_its_own_transaction(
    db_session, tmp_path, printer_factory, monkeypatch, spool_sessions
):
    source, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    receipt = Receipt()

    items, _queue = await _printer_add(db_session, source, queue, receipt)

    ids = sorted(item.id for item in items)
    assert receipt.claimed == 1 and receipt.recorded == (ids, [])
    assert len(receipt.claimed_sha) == 64
    assert await _marker(spool_sessions) == ",".join(map(str, ids))


async def test_the_auto_writer_records_the_exact_job_ids_in_its_own_transaction(
    db_session, tmp_path, printer_factory, monkeypatch, spool_sessions
):
    source, _printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    receipt = Receipt()

    items = await _auto_add(db_session, source, receipt)

    ids = sorted(item.id for item in items)
    assert receipt.claimed == 1 and receipt.recorded == ([], ids)
    assert await _marker(spool_sessions) == ",".join(map(str, ids))


@pytest.mark.parametrize("writer", ["printer", "auto"])
async def test_a_claim_refused_as_already_published_commits_nothing(
    db_session, tmp_path, printer_factory, monkeypatch, spool_sessions, writer
):
    source, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    receipt = Receipt(refuse=AlreadyPublished("entry already published"))

    with pytest.raises(AlreadyPublished):
        if writer == "printer":
            await _printer_add(db_session, source, queue, receipt)
        else:
            await _auto_add(db_session, source, receipt)

    assert receipt.recorded is None
    assert await _count(spool_sessions, PrintQueueItem) == 0
    assert await _count(spool_sessions, AutoQueueItem) == 0
    assert await _count(spool_sessions, QueueSource) == 0
    assert await _marker(spool_sessions) is None


@pytest.mark.parametrize("writer", ["printer", "auto"])
async def test_a_receipt_that_cannot_be_written_takes_the_jobs_with_it(
    db_session, tmp_path, printer_factory, monkeypatch, spool_sessions, writer
):
    source, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    receipt = Receipt(fail_record=RuntimeError("receipt write failed"))

    with pytest.raises(RuntimeError):
        if writer == "printer":
            await _printer_add(db_session, source, queue, receipt)
        else:
            await _auto_add(db_session, source, receipt)

    assert await _count(spool_sessions, PrintQueueItem) == 0
    assert await _count(spool_sessions, AutoQueueItem) == 0
    assert await _marker(spool_sessions) is None


async def test_a_writer_without_a_receipt_is_unchanged(
    db_session, tmp_path, printer_factory, monkeypatch, spool_sessions
):
    source, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)

    items, _queue = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15, quantity=2), None
    )

    assert len(items) == 2
    assert await _marker(spool_sessions) is None
