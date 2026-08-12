"""Snapshots at print start and finish, and why they cannot skew the range report.

The counter is read at both edges of a print anyway, to compute per-print
energy. Keeping those two readings costs nothing and gives the date-range report
boundaries on real events instead of on whichever hour the background loop last
fired — a "today" total asked for straight after a print finished used to answer
with a counter up to an hour stale.

The question worth pinning is whether denser snapshots distort the total. They
cannot: ``_sum_snapshot_deltas`` takes ``endpoint - baseline`` per plug, not a
sum over consecutive pairs. These tests hold that shape still, because the day
someone rewrites it as a running sum is the day extra rows start double-counting.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.api.routes.archives import _sum_snapshot_deltas
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot
from backend.app.services.smart_plug_manager import smart_plug_manager

pytestmark = pytest.mark.asyncio


async def _plug(db) -> SmartPlug:
    plug = SmartPlug(name="bench", plug_type="zigbee", zigbee_ieee="a4:c1:38:00:00:00:00:01")
    db.add(plug)
    await db.commit()
    await db.refresh(plug)
    return plug


async def _snapshots(db, plug_id: int, points: list[tuple[datetime, float]]) -> None:
    for when, kwh in points:
        db.add(SmartPlugEnergySnapshot(plug_id=plug_id, recorded_at=when, lifetime_kwh=kwh))
    await db.commit()


async def test_extra_snapshots_do_not_change_the_range_total(db_session):
    """The whole question, asked directly.

    Same counter movement over the same day, recorded twice: once hourly, once
    with print-edge readings interleaved. The reported total must be identical —
    it is endpoint minus baseline, and the points between are irrelevant to it.
    """
    day = datetime(2026, 7, 31, tzinfo=timezone.utc)
    end = day.replace(hour=23, minute=59)
    plug = await _plug(db_session)

    # Hourly-ish: four points, 10.0 -> 11.5 across the day.
    await _snapshots(
        db_session,
        plug.id,
        [(day, 10.0), (day.replace(hour=6), 10.4), (day.replace(hour=12), 11.0), (day.replace(hour=18), 11.5)],
    )
    total_sparse, _ = await _sum_snapshot_deltas(db_session, dt_from=day, dt_to=end)

    # The identical movement — same first and last reading — recorded far more
    # densely, as print-edge snapshots would.
    for row in (await db_session.execute(select(SmartPlugEnergySnapshot))).scalars().all():
        await db_session.delete(row)
    await db_session.commit()
    steps = 40
    await _snapshots(
        db_session,
        plug.id,
        [(day + timedelta(minutes=30 * i), 10.0 + 1.5 * (i / steps)) for i in range(steps + 1)],
    )
    total_dense, _ = await _sum_snapshot_deltas(db_session, dt_from=day, dt_to=end)

    assert round(total_sparse, 3) == round(total_dense, 3) == 1.5


async def test_a_snapshot_at_the_end_makes_the_total_current(db_session):
    """The reason for doing this at all.

    Without the finish-time reading, a range ending now reports the counter as
    of the last hourly snapshot, so a print that just completed is missing from
    "today" until the loop next fires.
    """
    plug = await _plug(db_session)
    now = datetime.now(timezone.utc)
    await _snapshots(db_session, plug.id, [(now - timedelta(hours=2), 10.0), (now - timedelta(minutes=55), 10.2)])

    # The window has to close slightly ahead of the wall clock: the snapshot the
    # hook writes is stamped when it runs, i.e. after `now` was captured here.
    window = {"dt_from": now - timedelta(hours=3), "dt_to": now + timedelta(minutes=1)}

    stale, _ = await _sum_snapshot_deltas(db_session, **window)
    assert round(stale, 3) == 0.2, "only what the hourly loop happened to catch"

    # What the print-end hook now adds.
    assert await smart_plug_manager.record_energy_snapshot(db_session, plug.id, 10.9)
    await db_session.commit()

    fresh, _ = await _sum_snapshot_deltas(db_session, **window)
    assert round(fresh, 3) == 0.9, "the just-finished print is included immediately"


async def test_a_reading_of_none_is_not_recorded(db_session):
    """A plug with no lifetime counter must not put NULL-ish rows in a table
    whose whole purpose is cumulative arithmetic."""
    plug = await _plug(db_session)

    assert await smart_plug_manager.record_energy_snapshot(db_session, plug.id, None) is False
    await db_session.commit()

    rows = (await db_session.execute(select(SmartPlugEnergySnapshot))).scalars().all()
    assert rows == []


async def test_it_does_not_commit_on_its_own(db_session):
    """The callers own a transaction holding the archive row this reading belongs
    to. A snapshot that survived while the energy figure beside it rolled back
    would be worse than no snapshot."""
    plug = await _plug(db_session)

    await smart_plug_manager.record_energy_snapshot(db_session, plug.id, 12.3)
    await db_session.rollback()

    rows = (await db_session.execute(select(SmartPlugEnergySnapshot))).scalars().all()
    assert rows == []


async def test_a_counter_reset_is_clamped_not_subtracted(db_session):
    """Why the finish hook records before the negative-delta check: the reading
    is true even when the per-print figure derived from it is not, and the report
    needs it as the new baseline."""
    plug = await _plug(db_session)
    now = datetime.now(timezone.utc)
    await _snapshots(
        db_session,
        plug.id,
        [(now - timedelta(hours=2), 40.0), (now - timedelta(minutes=5), 0.2)],  # plug reset
    )

    total, _ = await _sum_snapshot_deltas(db_session, dt_from=now - timedelta(hours=3), dt_to=now)

    assert total == 0.0, "a counter that went backwards contributes zero, never a negative"
