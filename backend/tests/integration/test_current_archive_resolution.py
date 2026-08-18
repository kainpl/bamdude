"""Which archive the printer's status says is printing right now.

Written after a live miss: 3DP-030-101 (an A1 mini) was printing a job started
outside BamDude, the card showed it, and the "copy this queue" button was not
there. Measured cause — every archive on that machine carried
``subtask_id IS NULL`` while genuinely printing, and the resolution keyed on
``subtask_id`` alone. Prints started from the printer's screen or straight from
the slicer have no cloud subtask, and those are the same prints that have no
queue row, so nothing else named them either.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.api.routes.printers import resolve_current_archive_id
from backend.app.models.archive import PrintArchive


async def _archive(db, *, printer_id: int, status: str, subtask_id: str | None, minutes_ago: int) -> PrintArchive:
    row = PrintArchive(
        printer_id=printer_id,
        file_path="",
        file_size=0,
        print_name=f"print-{minutes_ago}",
        filename=f"print-{minutes_ago}.gcode.3mf",
        status=status,
        subtask_id=subtask_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_with_no_subtask_is_still_found(db_session):
    """The live miss, as a test: a screen-started job on an A1 mini."""
    open_archive = await _archive(db_session, printer_id=9, status="printing", subtask_id=None, minutes_ago=5)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, None) == open_archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_subtask_still_wins_when_there_is_one(db_session):
    """A cloud print names its archive exactly; that must keep working."""
    exact = await _archive(db_session, printer_id=9, status="completed", subtask_id="abc", minutes_ago=5)
    await _archive(db_session, printer_id=9, status="printing", subtask_id=None, minutes_ago=1)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, "abc") == exact.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unmatched_subtask_falls_through_rather_than_giving_up(db_session):
    """A subtask we have no archive for is not an answer — it is a miss, and the
    open archive is still the right one."""
    open_archive = await _archive(db_session, printer_id=9, status="printing", subtask_id=None, minutes_ago=1)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, "never-seen") == open_archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_stale_open_archive_loses_to_the_current_one(db_session):
    """A print that crashed without its completion handler leaves ``printing``
    behind for good. Newest-first is what keeps that harmless."""
    await _archive(db_session, printer_id=9, status="printing", subtask_id=None, minutes_ago=6000)
    current = await _archive(db_session, printer_id=9, status="printing", subtask_id=None, minutes_ago=2)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, None) == current.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_another_printers_print_is_never_the_answer(db_session):
    await _archive(db_session, printer_id=7, status="printing", subtask_id=None, minutes_ago=1)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, None) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_finished_print_is_not_the_answer(db_session):
    """The caller only asks while the printer is busy, so an archive that has
    already closed cannot be what is on the bed."""
    await _archive(db_session, printer_id=9, status="completed", subtask_id=None, minutes_ago=1)
    await db_session.commit()

    assert await resolve_current_archive_id(db_session, 9, None) is None
