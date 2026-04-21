"""Tests for the energy snapshot delta arithmetic + per-print restart resilience.

Covers the upstream #941 refactor:
- `_sum_snapshot_deltas` baseline/endpoint pick + counter-reset clamp
- `energy_data_warming_up` flag when no pre-range baseline exists
- per-print `energy_start_kwh` survives a fresh DB session (restart resilience)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def two_plugs(db_session):
    """Two enabled smart plugs for tests that exercise the multi-plug path."""
    from backend.app.models.smart_plug import SmartPlug

    p1 = SmartPlug(name="Plug 1", plug_type="tasmota", ip_address="10.0.0.1", enabled=True)
    p2 = SmartPlug(name="Plug 2", plug_type="tasmota", ip_address="10.0.0.2", enabled=True)
    db_session.add_all([p1, p2])
    await db_session.commit()
    await db_session.refresh(p1)
    await db_session.refresh(p2)
    return p1, p2


@pytest.fixture
async def one_plug(db_session):
    """Single enabled smart plug for tests that don't need multi-plug semantics."""
    from backend.app.models.smart_plug import SmartPlug

    plug = SmartPlug(name="Plug 1", plug_type="tasmota", ip_address="10.0.0.1", enabled=True)
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)
    return plug


async def _add_snapshot(db, plug_id: int, recorded_at: datetime, lifetime_kwh: float):
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    db.add(SmartPlugEnergySnapshot(plug_id=plug_id, recorded_at=recorded_at, lifetime_kwh=lifetime_kwh))
    await db.commit()


@pytest.mark.asyncio
async def test_snapshot_delta_baseline_and_endpoint(db_session, two_plugs):
    """Picks the most recent snapshot at-or-before each bound and returns the difference."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    p1, p2 = two_plugs
    base = datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)
    # Plug 1: 100 → 105 (5 kWh in range)
    await _add_snapshot(db_session, p1.id, base - timedelta(hours=2), 100.0)
    await _add_snapshot(db_session, p1.id, base + timedelta(hours=12), 102.0)
    await _add_snapshot(db_session, p1.id, base + timedelta(hours=23), 105.0)
    # Plug 2: 50 → 53 (3 kWh in range)
    await _add_snapshot(db_session, p2.id, base - timedelta(hours=1), 50.0)
    await _add_snapshot(db_session, p2.id, base + timedelta(hours=20), 53.0)

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=base,
        dt_to=base + timedelta(hours=24),
    )
    assert total == pytest.approx(5.0 + 3.0)
    assert warming is False


@pytest.mark.asyncio
async def test_snapshot_delta_clamps_counter_reset(db_session, one_plug):
    """A negative delta (counter reset/replacement) clamps to zero rather than going negative."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    base = datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)
    await _add_snapshot(db_session, one_plug.id, base - timedelta(hours=1), 500.0)
    await _add_snapshot(db_session, one_plug.id, base + timedelta(hours=12), 10.0)  # reset

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=base,
        dt_to=base + timedelta(hours=24),
    )
    assert total == 0.0
    assert warming is False


@pytest.mark.asyncio
async def test_snapshot_warming_up_when_no_baseline(db_session, two_plugs):
    """Falls back to earliest snapshot and flags warming_up when no pre-range baseline."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    p1, _ = two_plugs
    base = datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)
    # Snapshots only AFTER dt_from
    await _add_snapshot(db_session, p1.id, base + timedelta(hours=2), 100.0)
    await _add_snapshot(db_session, p1.id, base + timedelta(hours=20), 102.5)

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=base,
        dt_to=base + timedelta(hours=24),
    )
    # Endpoint - earliest = 2.5 (undercounts the pre-snapshot portion of the range)
    assert total == pytest.approx(2.5)
    assert warming is True


@pytest.mark.asyncio
async def test_snapshot_warming_up_when_no_snapshots_at_all(db_session, two_plugs):
    """A plug with zero snapshots contributes nothing but flips warming_up."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    p1, p2 = two_plugs
    base = datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)
    # Only p1 has data.
    await _add_snapshot(db_session, p1.id, base - timedelta(hours=1), 10.0)
    await _add_snapshot(db_session, p1.id, base + timedelta(hours=10), 12.0)
    # p2 has nothing → flips warming_up but adds 0.

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=base,
        dt_to=base + timedelta(hours=24),
    )
    assert total == pytest.approx(2.0)
    assert warming is True


@pytest.mark.asyncio
async def test_snapshot_no_plugs_returns_empty(db_session):
    """No plugs configured at all → 0 kWh, not warming."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=datetime(2026, 4, 10, tzinfo=timezone.utc),
        dt_to=datetime(2026, 4, 11, tzinfo=timezone.utc),
    )
    assert total == 0.0
    assert warming is False


@pytest.mark.asyncio
async def test_snapshot_endpoint_windowing(db_session, one_plug):
    """Endpoint picks the latest snapshot at-or-before dt_to (later snapshots ignored)."""
    from backend.app.api.routes.archives import _sum_snapshot_deltas

    base = datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)
    await _add_snapshot(db_session, one_plug.id, base - timedelta(hours=1), 100.0)
    await _add_snapshot(db_session, one_plug.id, base + timedelta(hours=10), 105.0)
    # Past dt_to → must be ignored
    await _add_snapshot(db_session, one_plug.id, base + timedelta(hours=30), 200.0)

    total, warming = await _sum_snapshot_deltas(
        db_session,
        dt_from=base,
        dt_to=base + timedelta(hours=24),
    )
    assert total == pytest.approx(5.0)
    assert warming is False


@pytest.mark.asyncio
async def test_energy_start_persists_across_session(db_session):
    """`energy_start_kwh` survives a fresh DB session (restart-resilient #941)."""
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        printer_id=None,
        filename="restart-test.3mf",
        file_path="/tmp/restart-test.3mf",
        file_size=1024,
        print_name="Restart Test",
        status="printing",
        energy_start_kwh=42.5,
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    # Drop everything from the identity map to simulate a fresh process boot.
    db_session.expunge_all()

    fetched = await db_session.get(PrintArchive, archive_id)
    assert fetched is not None
    assert fetched.energy_start_kwh == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_snapshot_task_lifecycle_idempotent_start():
    """Starting the snapshot loop twice must leave only one task."""
    from backend.app.services.smart_plug_manager import SmartPlugManager

    mgr = SmartPlugManager()
    mgr.start_scheduler()
    first_snapshot = mgr._snapshot_task
    first_scheduler = mgr._scheduler_task
    mgr.start_scheduler()  # idempotent
    assert mgr._snapshot_task is first_snapshot
    assert mgr._scheduler_task is first_scheduler
    mgr.stop_scheduler()
    assert mgr._snapshot_task is None
    assert mgr._scheduler_task is None
