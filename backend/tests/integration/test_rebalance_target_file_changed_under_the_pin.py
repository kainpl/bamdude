"""The rebalance moves a job onto its target file only while that file is the one its copy was taken from (G03 B,
A28; acceptance TA35 (d), (e)).

Between the move's snapshot publication and its lock of the target file F -- inside the blob's pin, before the order
and router rows -- F is trashed, purged, or purged and replaced by another library row that takes F's id. The move
reads F again ``FOR KEY SHARE`` and compares its incarnation with the one the capture copied: each case is
``source_unreadable``, and neither the converted row nor any companion is written.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.services import queue_rebalance, queue_sources
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 -- fixtures by name
    _live,
    clean_spool,
    petg_farm,
)
from backend.tests.integration.test_rebalance_moves_one_job_once import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def change_the_target(test_engine, file_id: int, how: str) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as other:
        if how == "trashed":
            await other.execute(
                update(LibraryFile).where(LibraryFile.id == file_id).values(deleted_at=datetime.now(timezone.utc))
            )
        else:
            original = await other.get(LibraryFile, file_id)
            kept = {"filename": original.filename, "file_path": original.file_path, "created_at": original.created_at}
            await other.execute(delete(LibraryFile).where(LibraryFile.id == file_id))
            if how == "replaced":
                # Another row under F's id -- same path, same creation time (the review's probe): only the
                # incarnation tells it apart.
                other.add(LibraryFile(id=file_id, file_type="3mf", file_size=1, **kept))
        await other.commit()


@pytest.mark.parametrize("how", ["trashed", "purged", "replaced"])
async def test_a_target_file_changed_after_the_copy_moves_nothing(
    how, db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    item_id, home_file_id, target_id = farm.item.id, farm.home_file.id, farm.alt_file.id
    real_pin = queue_sources.pin
    changed: list[str] = []

    @asynccontextmanager
    async def pin(blob_id):
        async with real_pin(blob_id) as value:
            if not changed:
                changed.append(how)
                await change_the_target(test_engine, target_id, how)
            yield value

    monkeypatch.setattr(queue_sources, "pin", pin)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert changed == [how], "the move reached its pin"
    assert result.skipped == [(item_id, "source_unreadable")], result.skipped
    assert (result.converted, result.created) == (0, 0)
    async with AsyncSession(test_engine, expire_on_commit=False) as fresh:
        rows = list(await fresh.scalars(select(AutoQueueItem).order_by(AutoQueueItem.id)))
    assert [(row.id, row.library_file_id, row.rebalanced_at) for row in rows] == [(item_id, home_file_id, None)]
