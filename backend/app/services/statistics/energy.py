"""Energy figures for the statistics overview.

Two ways a range total is summed, chosen by ``energy_tracking_mode``: the
live totals of every plug, or hourly snapshot deltas clamped on a counter
reset. Moved verbatim out of ``routes/archives.py`` on 2026-09-17;
``routes/statistics.py`` is the only caller. Unit tests:
``tests/unit/test_energy_snapshots.py``,
``tests/unit/services/test_energy_snapshot_on_print_edges.py``.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.smart_plug_manager import smart_plug_manager


async def sum_live_plug_totals(db: AsyncSession) -> float:
    """Sum the live lifetime counter from every smart plug.

    Used for all-time "total consumption" mode. Only the current value is
    available so this can't be date-filtered - use `sum_snapshot_deltas` for
    that case.
    """
    from backend.app.models.smart_plug import SmartPlug

    plugs_result = await db.execute(select(SmartPlug))
    plugs = list(plugs_result.scalars().all())

    # Resolved per plug rather than branched on by hand. The chain this replaces
    # had no ``else``, so a plug type it predated matched nothing and silently
    # contributed zero to the total — the sibling of main.py::_get_plug_energy,
    # whose own chain defaulted to Tasmota instead. Going through the manager
    # means a new plug type is one line there. It also configures the Home
    # Assistant service itself, which is why the ha_url/ha_token preamble that
    # used to sit here is gone.
    total = 0.0
    for plug in plugs:
        service = await smart_plug_manager.get_service_for_plug(plug, db)
        energy = await service.get_energy(plug)
        if not energy:
            continue
        # REST reports only a daily figure; every other driver reports a
        # lifetime one. That asymmetry is real — a REST plug has no lifetime
        # counter — so it survives the collapse of the per-type chain rather
        # than being tidied into a single key, which would drop REST plugs out
        # of the totals entirely.
        value = energy.get("total")
        if value is None and plug.plug_type == "rest":
            value = energy.get("today")
        if value is not None:
            total += value
    return total


async def sum_snapshot_deltas(
    db: AsyncSession,
    *,
    dt_from: datetime | None,
    dt_to: datetime | None,
) -> tuple[float, bool]:
    """Sum per-plug energy consumption over a date range using hourly snapshots.

    For each plug:
      * baseline  = last snapshot at or before `dt_from` (ideal)
                    - if missing, fall back to the earliest snapshot ever
                      recorded for the plug and flag the result as warming up.
      * endpoint  = last snapshot at or before `dt_to` (or most recent overall)
      * delta     = max(0, endpoint - baseline)  - clamp counter resets to 0.

    Returns (total_kwh, warming_up). `warming_up = True` means at least one plug
    had no baseline before `dt_from` (fresh install or fresh upgrade), so the
    result undercounts the beginning of the range.
    """
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    plug_ids_result = await db.execute(select(SmartPlug.id))
    plug_ids = [row[0] for row in plug_ids_result.all()]
    if not plug_ids:
        return 0.0, False

    total = 0.0
    warming_up = False
    for plug_id in plug_ids:
        baseline: float | None = None
        if dt_from is not None:
            baseline_q = await db.execute(
                select(SmartPlugEnergySnapshot.lifetime_kwh)
                .where(
                    SmartPlugEnergySnapshot.plug_id == plug_id,
                    SmartPlugEnergySnapshot.recorded_at <= dt_from,
                )
                .order_by(SmartPlugEnergySnapshot.recorded_at.desc())
                .limit(1)
            )
            baseline = baseline_q.scalar()
        if baseline is None:
            # No snapshot before range start - fall back to the earliest
            # snapshot ever recorded. Result undercounts the pre-first-snapshot
            # portion of the range; signal that to the frontend.
            earliest_q = await db.execute(
                select(SmartPlugEnergySnapshot.lifetime_kwh)
                .where(SmartPlugEnergySnapshot.plug_id == plug_id)
                .order_by(SmartPlugEnergySnapshot.recorded_at.asc())
                .limit(1)
            )
            baseline = earliest_q.scalar()
            if baseline is None:
                # No snapshots at all for this plug yet.
                warming_up = True
                continue
            warming_up = True

        endpoint_conditions = [SmartPlugEnergySnapshot.plug_id == plug_id]
        if dt_to is not None:
            endpoint_conditions.append(SmartPlugEnergySnapshot.recorded_at <= dt_to)
        endpoint_q = await db.execute(
            select(SmartPlugEnergySnapshot.lifetime_kwh)
            .where(*endpoint_conditions)
            .order_by(SmartPlugEnergySnapshot.recorded_at.desc())
            .limit(1)
        )
        endpoint = endpoint_q.scalar()
        if endpoint is None:
            continue

        total += max(0.0, endpoint - baseline)

    return total, warming_up
