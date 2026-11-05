"""A connector write checks its proof under the lock it writes under (H0 review r2), on independent connections.

The default test database shares one connection between every session, so a second writer can never be blocked
there and this property is invisible. Here the database is a file (or, through the PostgreSQL module, a disposable
embedded server): a replacement of the operation's row that starts after the proof was checked must wait for the
write, never slip in between the check and the write.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from backend.app.models.connector_submission import ConnectorSubmission, ConnectorSubmissionEntry
from backend.app.services import connector_submissions as cs
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401 - a file, independent connections
from backend.tests.integration.test_connector_submissions import (  # noqa: F401 - fixtures
    _body,
    _clean_spool_state,
    _printer_entry,
    farm,
    sessions,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _replace_and_observe(sessions, submission_id: int) -> list[str]:
    """Replace the operation's row (rotated namespace, another key's other operation) and report the entries' state
    as this writer saw it -- the order writers committed in."""
    import uuid

    async with sessions() as db:
        epoch = await cs.rotate_storage_epoch(db)
        await db.execute(
            update(ConnectorSubmission)
            .where(ConnectorSubmission.id == submission_id)
            .values(operation_id=str(uuid.uuid4()), api_key_identity=str(uuid.uuid4()), storage_epoch=epoch)
        )
        seen = (
            await db.execute(
                select(ConnectorSubmissionEntry.state).where(ConnectorSubmissionEntry.submission_id == submission_id)
            )
        ).scalars().all()  # fmt: skip
        await db.commit()
        return list(seen)


def _replace_after_the_check(monkeypatch, sessions, submission_id: int) -> dict:
    """Right after the write's proof check, start a replacement from another connection and give it time to land."""
    original = cs._verified
    state: dict = {}

    async def verified_then_race(db, proof):
        row = await original(db, proof)
        if "task" not in state:
            state["task"] = asyncio.create_task(_replace_and_observe(sessions, submission_id))
            await asyncio.sleep(0.5)  # unlocked, the replacement commits here
        return row

    monkeypatch.setattr(cs, "_verified", verified_then_race)
    return state


async def _states(sessions, submission_id):
    async with sessions() as db:
        return list(
            (
                await db.execute(
                    select(ConnectorSubmissionEntry.state).where(
                        ConnectorSubmissionEntry.submission_id == submission_id
                    )
                )
            ).scalars()
        )


async def test_a_cancel_holds_its_row_from_the_check_to_the_write(farm, sessions, monkeypatch):
    registered = await cs.register(farm["actor"], _body(farm, _printer_entry(farm, quantity=1)))
    race = _replace_after_the_check(monkeypatch, sessions, registered.submission_id)

    await cs.cancel(registered.proof)
    seen_by_the_replacement = await asyncio.wait_for(race["task"], 30)

    # The replacement waited for the cancel: it found the cancel already written, on the proven operation.
    assert seen_by_the_replacement == ["cancelled"]


async def test_a_resume_holds_its_row_from_the_check_to_the_write(farm, sessions, monkeypatch):
    registered = await cs.register(farm["actor"], _body(farm, _printer_entry(farm, quantity=1)))
    async with sessions() as db:
        await db.execute(
            update(ConnectorSubmissionEntry)
            .where(ConnectorSubmissionEntry.submission_id == registered.submission_id)
            .values(state="preparing")
        )
        await db.commit()
    race = _replace_after_the_check(monkeypatch, sessions, registered.submission_id)

    assert await cs.make_resumable(registered.proof) == 1
    seen_by_the_replacement = await asyncio.wait_for(race["task"], 30)

    assert seen_by_the_replacement == ["pending"]
    assert await _states(sessions, registered.submission_id) == ["pending"]
