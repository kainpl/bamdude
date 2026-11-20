"""The swap refresh locks only job rows of the lanes it holds, and only ``FOR NO KEY UPDATE`` (G03 B, A31 H1;
acceptance TA38, both dialects).

A refresh whose request names a printer row that moved to another printer's queue after the preflight read its lanes:
the row is read unlocked and the answer is ``jobs_changed``, nothing written. The statement log shows every job-row
lock restricted to the held lanes (``queue_id IN``) and none of them ``FOR UPDATE``.
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import swap_compatibility as sc, swap_job_refresh as jobs
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_swap_job_refresh import (
    a_declared_archive,
    a_job,
    a_printer_queue,
    a_user,
    apply,
    by_id,
    listing,
    ref,
    row,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def factory(test_engine, monkeypatch):
    """The refresh scenarios' sessions on the dual-dialect engine (``test_swap_job_refresh`` binds them to SQLite)."""
    from backend.app.services import archive_write_scope, queue_ops, queue_sources

    made = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    queue_sources._reset_state()
    archive_write_scope._archive_locks.clear()
    queue_ops._queue_claim_locks.clear()
    yield made
    queue_sources._reset_state()
    archive_write_scope._archive_locks.clear()
    queue_ops._queue_claim_locks.clear()


async def test_a_job_moved_after_the_preflight_is_never_locked_and_answers_jobs_changed(
    test_engine, factory, tmp_path, monkeypatch
):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    other = await a_printer_queue(factory, "SN2")
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    real = jobs._guard_actor

    async def moved_meanwhile(session, actor):
        async with factory() as db:
            (await db.get(PrintQueueItem, job_id)).queue_id = other
            await db.commit()
        return await real(session, actor)

    statements: list[str] = []

    def seen_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    monkeypatch.setattr(jobs, "_guard_actor", moved_meanwhile)
    event.listen(test_engine.sync_engine, "before_cursor_execute", seen_statement)
    try:
        with pytest.raises(HTTPException) as moved:
            await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", seen_statement)
    assert moved.value.status_code == 409 and moved.value.detail["error"] == "jobs_changed"
    current = await row(factory, "queue", job_id)
    assert current.queue_id == other
    assert sc.snapshot_fact(current.source_snapshot).value is None, "nothing written"

    job_locks = [s for s in statements if re.search(r"FROM print_queue\b", s) and re.search(r"\bFOR\b.*\bUPDATE\b", s)]
    if test_engine.dialect.name == "postgresql":
        assert job_locks, "the refresh locked its held lanes' rows"
    for statement in job_locks:
        assert "FOR NO KEY UPDATE" in statement, statement
        assert re.search(r"print_queue\.queue_id IN", statement), f"a job-row lock outside the held lanes: {statement}"
