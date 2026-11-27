"""Energy figures for the statistics overview.

Two ways a range total is summed, chosen by ``energy_tracking_mode``: the
live totals of every plug, or hourly snapshot deltas clamped on a counter
reset. Their cost is at the price of its own time (m245, the second half of
this module). Moved verbatim out of ``routes/archives.py`` on 2026-09-17;
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
    return sum((value for _plug, value, _lifetime in await live_plug_readings(db)), 0.0)


async def live_plug_readings(db: AsyncSession, printer_scope: frozenset[int] | None = None) -> list[tuple]:
    """Every plug's live energy figure: ``(plug, kWh, is_lifetime_counter)``, plugs without one left out.

    ``is_lifetime_counter`` is False for a REST plug that reports only a daily figure -- it is in the
    all-time sum (see below) but has no counter history to cost it along. ``printer_scope`` keeps the
    plugs of those printers and the plugs bound to none (N62; None = every plug).
    """
    from backend.app.core.printer_scope import where as printer_scope_where
    from backend.app.models.smart_plug import SmartPlug

    in_scope = printer_scope_where(printer_scope, SmartPlug.printer_id)
    plugs_result = await db.execute(select(SmartPlug).where(*([in_scope] if in_scope is not None else [])))
    plugs = list(plugs_result.scalars().all())

    # Resolved per plug rather than branched on by hand. The chain this replaces
    # had no ``else``, so a plug type it predated matched nothing and silently
    # contributed zero to the total — the sibling of main.py::_get_plug_energy,
    # whose own chain defaulted to Tasmota instead. Going through the manager
    # means a new plug type is one line there. It also configures the Home
    # Assistant service itself, which is why the ha_url/ha_token preamble that
    # used to sit here is gone.
    readings: list[tuple] = []
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
        lifetime = value is not None
        if value is None and plug.plug_type == "rest":
            value = energy.get("today")
        if value is not None:
            readings.append((plug, value, lifetime))
    return readings


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


# ── Cost at the price of its own time (m245) ─────────────────────────────────
#
# The kWh figures above stay exactly what they were. What changed is their money: it used to be the
# figure times TODAY's price, so a new tariff re-costed every past month. Now each plug's figure is
# costed at the average price of its own counter steps (``energy_price.cost_of_steps``), read from
# the same snapshots the figure comes from. A fixed number of queries whatever the range holds --
# the plugs, the price history, the snapshots (one statement, streamed), and for a range the
# baselines -- and the arithmetic runs off the event loop.

_SNAPSHOT_BATCH = 5000


async def _snapshot_rows(db: AsyncSession, stmt) -> list[tuple]:
    """A snapshot statement's rows, fetched in batches so a long history never holds the loop."""
    rows: list[tuple] = []
    result = await db.stream(stmt.execution_options(yield_per=_SNAPSHOT_BATCH))
    async for batch in result.partitions():
        rows.extend(tuple(row) for row in batch)
    return rows


def _by_plug(rows) -> dict[int, list[tuple[datetime, float]]]:
    """``(plug_id, recorded_at, lifetime_kwh)`` rows, already in time order, as points per plug."""
    points: dict[int, list[tuple[datetime, float]]] = {}
    for plug_id, recorded_at, lifetime_kwh in rows:
        points.setdefault(plug_id, []).append((recorded_at, float(lifetime_kwh)))
    return points


def _steps_per_source(points, since, history):
    """``cost_of_steps`` over a plug's points, one run per measurement source.

    A snapshot row carries no source token; the readers' rule is the cutoff: a row at or before the
    plug's ``measurement_source_since`` is an earlier source's. The step ACROSS the cutoff joins two
    different meters and is no step at all.
    """
    from backend.app.services import energy_price

    if since is None:
        return energy_price.cost_of_steps(points, history)
    earlier = [point for point in points if point[0] <= since]
    current = [point for point in points if point[0] > since]
    return energy_price.cost_of_steps(earlier, history) + energy_price.cost_of_steps(current, history)


def _range_figures(plugs, baselines, in_range, history, dt_from) -> tuple[float, float, bool]:
    """Per plug ``max(0, endpoint - baseline)`` -- ``sum_snapshot_deltas``'s figure, point for point --
    and that figure at the average price of the plug's steps between the two."""
    from backend.app.services import energy_price

    total_kwh = total_cost = 0.0
    warming_up = False
    for plug_id, since in plugs:
        points = in_range.get(plug_id, [])
        baseline = baselines.get(plug_id)
        if baseline is None:
            # No snapshot before the range: the earliest one stands in, and the result undercounts.
            warming_up = True
            if not points:
                continue
        else:
            points = [baseline, *points]
        kwh = max(0.0, points[-1][1] - points[0][1])
        steps = _steps_per_source(points, since, history)
        start_price = energy_price.price_at(history, dt_from if dt_from is not None else points[0][0])
        total_kwh += kwh
        total_cost += kwh * energy_price.price_of(steps, start_price)
    return total_kwh, total_cost, warming_up


async def range_energy_and_cost(
    db: AsyncSession,
    *,
    dt_from: datetime | None,
    dt_to: datetime | None,
    printer_scope: frozenset[int] | None = None,
) -> tuple[float, float, bool]:
    """``sum_snapshot_deltas``'s total, its cost at the price of its own time, and ``warming_up``.

    Four queries: the plugs, the price history, each plug's baseline (its last snapshot at or before
    ``dt_from``) and the snapshots inside the range.
    """
    import asyncio

    from sqlalchemy import func

    from backend.app.core.printer_scope import where as printer_scope_where
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot as Snapshot
    from backend.app.services import energy_price

    dt_from = energy_price.naive_utc(dt_from) if dt_from is not None else None
    dt_to = energy_price.naive_utc(dt_to) if dt_to is not None else None
    # N62: the plugs of the printers in scope, and those bound to no printer.
    in_scope = printer_scope_where(printer_scope, SmartPlug.printer_id)
    plugs = [
        (plug_id, energy_price.naive_utc(since) if since is not None else None)
        for plug_id, since in (
            await db.execute(
                select(SmartPlug.id, SmartPlug.measurement_source_since).where(
                    *([in_scope] if in_scope is not None else [])
                )
            )
        ).all()
    ]
    if not plugs:
        return 0.0, 0.0, False
    history = await energy_price.load_history(db)

    baselines: dict[int, tuple[datetime, float]] = {}
    if dt_from is not None:
        newest = (
            select(Snapshot.plug_id, func.max(Snapshot.recorded_at).label("at"))
            .where(Snapshot.recorded_at <= dt_from)
            .group_by(Snapshot.plug_id)
            .subquery()
        )
        rows = await db.execute(
            select(Snapshot.plug_id, Snapshot.recorded_at, Snapshot.lifetime_kwh)
            .join(newest, (Snapshot.plug_id == newest.c.plug_id) & (Snapshot.recorded_at == newest.c.at))
            .order_by(Snapshot.plug_id, Snapshot.id)
        )
        for plug_id, recorded_at, lifetime_kwh in rows:
            baselines[plug_id] = (recorded_at, float(lifetime_kwh))  # two rows of one instant: the later id

    inside = select(Snapshot.plug_id, Snapshot.recorded_at, Snapshot.lifetime_kwh)
    if dt_from is not None:
        inside = inside.where(Snapshot.recorded_at > dt_from)
    if dt_to is not None:
        inside = inside.where(Snapshot.recorded_at <= dt_to)
    in_range = _by_plug(await _snapshot_rows(db, inside.order_by(Snapshot.plug_id, Snapshot.recorded_at, Snapshot.id)))
    return await asyncio.to_thread(_range_figures, plugs, baselines, in_range, history, dt_from)


def _all_time_cost(readings, points_by_plug, history, now) -> float:
    from backend.app.services import energy_price

    current_price = energy_price.price_at(history, now)
    baseline_price = history[0].price
    cost = 0.0
    for plug_id, value, lifetime in readings:
        if not lifetime:
            cost += value * current_price  # a daily figure: it is today's energy
            continue
        points = points_by_plug.get(plug_id)
        if not points:
            cost += value * baseline_price  # a counter nobody ever snapshotted: nothing dates its energy
            continue
        # What the counter held when it was first seen costs what the price was then; the steps
        # between snapshots cost the price of their own start; what came since the last snapshot
        # costs the price now.
        first_at, first_kwh = points[0]
        head = energy_price.StepCost(
            max(0.0, first_kwh), max(0.0, first_kwh) * energy_price.price_at(history, first_at)
        )
        tail_kwh = max(0.0, value - points[-1][1])
        tail = energy_price.StepCost(tail_kwh, tail_kwh * current_price)
        priced = head + energy_price.cost_of_steps(points, history) + tail
        cost += value * energy_price.price_of(priced, baseline_price)
    return cost


async def live_totals_and_cost(db: AsyncSession, printer_scope: frozenset[int] | None = None) -> tuple[float, float]:
    """``sum_live_plug_totals``'s total and its cost at the price of its own time.

    A live counter is its plug's CURRENT measurement source, so only that source's snapshots (after
    its cutoff) can date its energy. Beyond the driver reads: the price history and one statement
    for the snapshots.
    """
    import asyncio

    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot as Snapshot
    from backend.app.services import energy_price

    readings = [
        (plug.id, float(value), lifetime) for plug, value, lifetime in await live_plug_readings(db, printer_scope)
    ]
    total = sum((value for _id, value, _lifetime in readings), 0.0)
    counters = [plug_id for plug_id, _value, lifetime in readings if lifetime]
    history = await energy_price.load_history(db)
    points: dict[int, list[tuple[datetime, float]]] = {}
    if counters:
        stmt = (
            select(Snapshot.plug_id, Snapshot.recorded_at, Snapshot.lifetime_kwh)
            .join(SmartPlug, SmartPlug.id == Snapshot.plug_id)
            .where(
                Snapshot.plug_id.in_(counters),
                SmartPlug.measurement_source_since.is_(None)
                | (Snapshot.recorded_at > SmartPlug.measurement_source_since),
            )
            .order_by(Snapshot.plug_id, Snapshot.recorded_at, Snapshot.id)
        )
        points = _by_plug(await _snapshot_rows(db, stmt))
    cost = await asyncio.to_thread(_all_time_cost, readings, points, history, energy_price.naive_utc())
    return total, cost
