"""before_flush event keeps PrintArchive.actual_time_seconds / time_accuracy in sync."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.archive import PrintArchive


def _archive(**kw):
    base = {
        "filename": "x.3mf",
        "file_path": "",
        "file_size": 0,
        "status": "printing",
        "started_at": datetime(2026, 1, 1, 10, 0, 0),
    }
    base.update(kw)
    return PrintArchive(**base)


@pytest.mark.asyncio
async def test_event_populates_on_completion(db_session):
    a = _archive(status="completed", completed_at=datetime(2026, 1, 1, 12, 0, 0), print_time_seconds=7000)
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    assert a.actual_time_seconds == 7200
    assert a.time_accuracy == round(7000 / 7200 * 100, 1)


@pytest.mark.asyncio
async def test_event_null_while_printing_then_set_on_complete(db_session):
    a = _archive(print_time_seconds=3600)
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    assert a.actual_time_seconds is None and a.time_accuracy is None

    a.status = "completed"
    a.completed_at = a.started_at + timedelta(seconds=3600)
    await db_session.commit()
    await db_session.refresh(a)
    assert a.actual_time_seconds == 3600 and a.time_accuracy == 100.0


@pytest.mark.asyncio
async def test_event_handles_aware_completed_at(db_session):
    a = _archive(
        status="completed",
        completed_at=datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc),  # aware vs naive started_at
        print_time_seconds=3600,
    )
    db_session.add(a)
    await db_session.commit()  # must not raise on mixed naive/aware
    await db_session.refresh(a)
    assert a.actual_time_seconds == 3600
