"""A claimed job that nobody reports on must not sit there looking like progress.

⚠️ ``claimed`` reads on screen as "printing". The case that matters is a bridge
dying mid-job — precisely the case where no further poll arrives, so nothing
else will ever notice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelDevice, LabelJob
from backend.app.services.label_reclaim import CLAIM_DEADLINE_SECONDS, MAX_ATTEMPTS, reclaim_stale_jobs


def _ago(minutes: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)


@pytest.fixture
async def a_device(db_session: AsyncSession) -> LabelDevice:
    device = LabelDevice(installation_id="sweeper-test", enabled=True, density=3)
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


async def _job(db_session: AsyncSession, device: LabelDevice, **overrides) -> LabelJob:
    fields = {
        "device_id": device.id,
        "width_mm": 50.0,
        "height_mm": 30.0,
        "image_png": b"x",
        "status": "claimed",
        "attempts": 0,
        "claimed_at": _ago(10),
    }
    fields.update(overrides)
    job = LabelJob(**fields)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_a_stale_claim_goes_back_to_the_queue_and_counts_the_attempt(db_session, a_device):
    job = await _job(db_session, a_device)
    assert await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3) == 1
    await db_session.refresh(job)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.claimed_at is None, "a requeued job that keeps its claim time would be swept again at once"


@pytest.mark.asyncio
async def test_past_the_attempt_cap_it_fails_visibly_instead_of_looping(db_session, a_device):
    """A job that requeues itself indefinitely is a queue that never empties and
    never says why.
    """
    job = await _job(db_session, a_device, attempts=3, error="printer said no")
    await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3)
    await db_session.refresh(job)
    assert job.status == "failed"


@pytest.mark.asyncio
async def test_the_last_thing_the_device_said_survives_the_sweep(db_session, a_device):
    """⚠️ Overwriting it with "gave up" would lose the only diagnosis there is."""
    job = await _job(db_session, a_device, attempts=3, error="printer said no")
    await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3)
    await db_session.refresh(job)
    assert job.error == "printer said no"


@pytest.mark.asyncio
async def test_a_job_that_never_reported_keeps_a_null_rather_than_an_invented_reason(db_session, a_device):
    """Nobody ever said, and that is the accurate answer."""
    job = await _job(db_session, a_device, attempts=3)
    await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.error is None


@pytest.mark.asyncio
async def test_a_fresh_claim_is_left_alone(db_session, a_device):
    """A printer mid-paper-feed is not stuck."""
    await _job(db_session, a_device, claimed_at=datetime.now(UTC).replace(tzinfo=None))
    assert await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3) == 0


@pytest.mark.asyncio
async def test_a_queued_job_is_not_touched(db_session, a_device):
    """It was never handed out; there is nothing to reclaim."""
    job = await _job(db_session, a_device, status="queued", claimed_at=None)
    assert await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3) == 0
    await db_session.refresh(job)
    assert job.attempts == 0


@pytest.mark.asyncio
async def test_a_printed_job_is_not_resurrected(db_session, a_device):
    """⚠️ It carries an old ``claimed_at`` by definition — reclaiming on the
    timestamp alone would reprint every label the farm has ever made.
    """
    job = await _job(db_session, a_device, status="printed", claimed_at=_ago(600))
    assert await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=3) == 0
    await db_session.refresh(job)
    assert job.status == "printed"


@pytest.mark.asyncio
async def test_a_mixed_batch_is_sorted_into_both_outcomes(db_session, a_device):
    young = await _job(db_session, a_device, attempts=0)
    old = await _job(db_session, a_device, attempts=MAX_ATTEMPTS)
    assert await reclaim_stale_jobs(db_session, older_than_seconds=300, max_attempts=MAX_ATTEMPTS) == 2
    await db_session.refresh(young)
    await db_session.refresh(old)
    assert young.status == "queued"
    assert old.status == "failed"


def test_the_deadline_is_generous_enough_for_a_slow_print():
    """A B1 feeding a batch is not a stuck B1. Reclaiming a job that is actually
    printing prints it twice.
    """
    assert CLAIM_DEADLINE_SECONDS >= 120
