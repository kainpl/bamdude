"""Energy is costed at the price of its own time (m245).

A print's cost follows its own source's counter step by step; an estimated print takes the
time-weighted price of its window; the plugs' totals in the statistics are costed from the same
snapshots their kWh comes from -- and a price changed LATER moves none of them.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.energy_price_history import EnergyPriceHistory
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.services.energy_price import BASELINE_AT
from backend.app.services.statistics import energy as statistics_energy

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TOKEN = "b" * 32


class _SessionProxy:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def factory(db_session, monkeypatch):
    monkeypatch.setattr(main, "async_session", lambda: _SessionProxy(db_session))


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


async def _prices(db, *changes):
    """A history: the first price is the baseline, the rest are (moment, price) changes."""
    baseline, *later = changes
    db.add(EnergyPriceHistory(effective_at=BASELINE_AT, price_per_kwh=baseline, source="baseline"))
    for moment, price in later:
        db.add(EnergyPriceHistory(effective_at=_naive(moment), price_per_kwh=price, source="manual"))
    # What the setting holds is the newest price -- and must not be what anything below is costed at.
    db.add(Settings(key="energy_cost_per_kwh", value=str(later[-1][1] if later else baseline)))
    await db.commit()


async def _change_price(db, price, moment=None):
    db.add(
        EnergyPriceHistory(
            effective_at=_naive(moment or datetime.now(timezone.utc)), price_per_kwh=price, source="manual"
        )
    )
    await db.commit()


async def _plug(db, printer_id=None, *, name="meter", since_days=30) -> SmartPlug:
    plug = SmartPlug(
        name=name,
        plug_type="mqtt",
        enabled=True,
        printer_id=printer_id,
        mqtt_power_topic="tele/meter/SENSOR",
        measurement_source_id=TOKEN,
        measurement_source_since=_naive(datetime.now(timezone.utc) - timedelta(days=since_days)),
    )
    db.add(plug)
    await db.commit()
    await db.refresh(plug)
    return plug


async def _snapshot(db, plug_id, moment, kwh):
    db.add(SmartPlugEnergySnapshot(plug_id=plug_id, recorded_at=_naive(moment), lifetime_kwh=kwh))
    await db.commit()


async def _archive(db, printer_id, plug, start, end, start_kwh):
    archive = PrintArchive(
        printer_id=printer_id,
        filename="a.3mf",
        file_path="x/a.3mf",
        file_size=1,
        status="completed",
        completed_at=_naive(end),
        energy_start_kwh=start_kwh,
        extra_data={
            "energy_plug_id": plug.id,
            "energy_source_id": plug.measurement_source_id,
            "energy_start_at": start.isoformat(),
        },
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


def _end_reading(total: float):
    async def fake(plug, db, *, force_read=False):
        return {"total": total}

    return patch.object(main, "_get_plug_energy", side_effect=fake)


# ---- a print ------------------------------------------------------------------------------------


async def test_a_print_through_a_price_change_is_costed_step_by_step(db_session, printer_factory, factory):
    printer = await printer_factory()
    plug = await _plug(db_session, printer.id)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    start = end - timedelta(hours=2)
    await _prices(db_session, 0.10, (start + timedelta(minutes=45), 0.30))
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.1)  # starts at 0.10
    await _snapshot(db_session, plug.id, start + timedelta(minutes=60), 10.2)  # starts at 0.30
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.5):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.5)
    # 10.0->10.1 at 0.10 (the step started before the change), 10.1->10.2 at 0.10 (so did this one),
    # 10.2->10.5 at 0.30. Whole at the price of its end it would have been 0.15.
    assert archive.energy_cost == pytest.approx(0.1 * 0.10 + 0.1 * 0.10 + 0.3 * 0.30, abs=1e-3)
    record = archive.extra_data["energy_provenance"]
    assert record["kind"] == "measured" and record["final"] is True
    assert record["tariff_method"] == "steps"
    assert record["tariff"] == pytest.approx(archive.energy_cost / archive.energy_kwh, abs=1e-2)

    # Settled: neither a new price nor a late callback re-costs it.
    settled = (archive.energy_kwh, archive.energy_cost, dict(archive.extra_data))
    await _change_price(db_session, 9.0)
    with _end_reading(10.5):
        await main._record_print_energy(archive.id, printer.id)
    await db_session.refresh(archive)
    assert (archive.energy_kwh, archive.energy_cost, archive.extra_data) == settled


async def test_a_print_with_no_point_between_its_readings_is_one_step_at_the_price_of_its_start(
    db_session, printer_factory, factory
):
    printer = await printer_factory()
    plug = await _plug(db_session, printer.id)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    start = end - timedelta(minutes=40)
    await _prices(db_session, 0.10, (start + timedelta(minutes=10), 0.30))
    await _snapshot(db_session, plug.id, start, 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.2):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2)
    assert archive.energy_cost == pytest.approx(0.2 * 0.10)
    record = archive.extra_data["energy_provenance"]
    assert (record["tariff"], record["tariff_method"]) == (0.10, "single")


async def test_snapshots_of_an_earlier_source_or_outside_the_window_are_not_the_prints_points(
    db_session, printer_factory, factory
):
    from backend.app.services import energy_price

    plug = await _plug(db_session)
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(hours=3), now - timedelta(hours=1)
    await _prices(db_session, 0.10, (start + timedelta(minutes=90), 0.50))
    await _snapshot(db_session, plug.id, start - timedelta(minutes=5), 50.0)  # before the window
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 99.0)  # another meter's, before the cutoff
    await _snapshot(db_session, plug.id, start + timedelta(minutes=100), 10.6)
    await _snapshot(db_session, plug.id, end + timedelta(minutes=5), 77.0)  # after the window
    history = await energy_price.load_history(db_session)

    price, method = await energy_price.print_price(
        db_session,
        plug.id,
        history,
        start_at=start,
        start_kwh=10.0,
        end_at=end,
        end_kwh=11.0,
        source_since=start + timedelta(minutes=60),
    )

    # 10.0->10.6 from the start (0.10), 10.6->11.0 from a point after the change (0.50).
    assert method == "steps"
    assert price == pytest.approx((0.6 * 0.10 + 0.4 * 0.50) / 1.0)


async def test_a_reset_inside_the_window_keeps_the_figure_and_scales_the_cost_to_it(db_session):
    from backend.app.services import energy_price

    plug = await _plug(db_session)
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(hours=3), now - timedelta(hours=1)
    await _prices(db_session, 0.10, (start + timedelta(minutes=60), 0.30))
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.4)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=90), 0.2)  # the counter was reset
    history = await energy_price.load_history(db_session)

    price, _method = await energy_price.print_price(
        db_session, plug.id, history, start_at=start, start_kwh=10.0, end_at=end, end_kwh=10.6
    )

    # Positive steps: 0.4 at 0.10, then (from 0.2) 10.4 at 0.30 -- the fall costs nothing. The answer
    # is their average price; the caller's own figure (0.6 kWh) is costed at it, unchanged.
    assert price == pytest.approx((0.4 * 0.10 + 10.4 * 0.30) / 10.8)


async def test_an_estimated_print_takes_the_time_weighted_price_of_its_window(db_session, printer_factory, factory):
    """A stalled counter has no steps to follow: an hour at 200 W over a flat counter is 0.2 kWh, costed
    at the average price of the hour -- half of it at 0.10, half at 0.30."""
    printer = await printer_factory()
    plug = await _plug(db_session, printer.id, since_days=2)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    start = end - timedelta(minutes=60)
    await _prices(db_session, 0.10, (start + timedelta(minutes=30), 0.30))
    moment = start - timedelta(minutes=1)
    while moment <= end + timedelta(minutes=1):
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=200.0, recorded_at=_naive(moment)))
        moment += timedelta(seconds=60)
    await db_session.commit()
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    record = archive.extra_data["energy_provenance"]
    assert record["kind"] == "estimated"
    assert archive.energy_kwh == pytest.approx(0.2, abs=1e-3)
    assert record["tariff_method"] == "window_average"
    assert record["tariff"] == pytest.approx(0.20, abs=1e-3)
    assert archive.energy_cost == pytest.approx(archive.energy_kwh * 0.20, abs=1e-3)


# ---- the plugs' totals in the statistics -------------------------------------------------------


def _day():
    """A whole UTC day three days ago, as the route hands its bounds over (aware UTC)."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=3)
    return start, start + timedelta(days=1)


async def test_a_range_is_costed_at_the_price_of_its_own_time_and_a_later_price_changes_nothing(db_session):
    day, next_day = _day()
    plug = await _plug(db_session)
    await _prices(db_session, 0.10, (day + timedelta(hours=15), 0.50))
    await _snapshot(db_session, plug.id, day - timedelta(hours=1), 100.0)  # the baseline
    await _snapshot(db_session, plug.id, day + timedelta(hours=10), 101.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=20), 103.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=22), 104.0)
    await _snapshot(db_session, plug.id, next_day + timedelta(hours=2), 110.0)  # after the range

    kwh, cost, warming_up = await statistics_energy.range_energy_and_cost(db_session, dt_from=day, dt_to=next_day)

    old_kwh, old_warming = await statistics_energy.sum_snapshot_deltas(db_session, dt_from=day, dt_to=next_day)
    assert (kwh, warming_up) == (pytest.approx(old_kwh), old_warming) == (pytest.approx(4.0), False)
    # 100->101 and 101->103 start before 15:00 (0.10); 103->104 starts at 20:00 (0.50).
    assert cost == pytest.approx(1 * 0.10 + 2 * 0.10 + 1 * 0.50)

    await _change_price(db_session, 7.0)  # today's tariff
    assert await statistics_energy.range_energy_and_cost(db_session, dt_from=day, dt_to=next_day) == (
        pytest.approx(4.0),
        pytest.approx(0.80),
        False,
    )


async def test_a_range_keeps_the_old_figures_for_a_plug_without_a_baseline_and_one_with_a_reset(db_session):
    day, next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=12), 0.30))
    fresh = await _plug(db_session, name="fresh")  # its first snapshot is inside the range
    await _snapshot(db_session, fresh.id, day + timedelta(hours=6), 5.0)
    await _snapshot(db_session, fresh.id, day + timedelta(hours=18), 6.0)
    reset = await _plug(db_session, name="reset")
    await _snapshot(db_session, reset.id, day - timedelta(hours=1), 50.0)
    await _snapshot(db_session, reset.id, day + timedelta(hours=6), 52.0)
    await _snapshot(db_session, reset.id, day + timedelta(hours=14), 51.0)  # fell back
    await _snapshot(db_session, reset.id, day + timedelta(hours=20), 51.5)
    silent = await _plug(db_session, name="silent")  # never snapshotted
    assert silent.id

    kwh, cost, warming_up = await statistics_energy.range_energy_and_cost(db_session, dt_from=day, dt_to=next_day)

    old_kwh, old_warming = await statistics_energy.sum_snapshot_deltas(db_session, dt_from=day, dt_to=next_day)
    assert (kwh, warming_up) == (pytest.approx(old_kwh), old_warming) == (pytest.approx(1.0 + 1.5), True)
    # fresh: 1 kWh, its one step starts at 06:00 -> 0.10.
    # reset: endpoint - baseline = 1.5 kWh, at the average price of its positive steps:
    #        2.0 at 0.10 and 0.5 at 0.30 (the step from the 14:00 point), the fall nothing.
    assert cost == pytest.approx(1.0 * 0.10 + 1.5 * ((2.0 * 0.10 + 0.5 * 0.30) / 2.5))


async def test_a_step_across_a_source_change_is_no_step(db_session):
    day, next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=12), 0.30))
    plug = await _plug(db_session)
    plug.measurement_source_since = _naive(day + timedelta(hours=11))  # another meter from 11:00 on
    await db_session.commit()
    await _snapshot(db_session, plug.id, day - timedelta(hours=1), 100.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=10), 101.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=13), 500.0)  # the new meter's first reading
    await _snapshot(db_session, plug.id, day + timedelta(hours=20), 503.0)

    kwh, cost, _warming = await statistics_energy.range_energy_and_cost(db_session, dt_from=day, dt_to=next_day)

    # The kWh figure is the old one, untouched (endpoint - baseline); its money is the figure at the
    # average price of the two meters' own steps -- 1 at 0.10 and 3 at 0.30 -- never of 101 -> 500.
    assert kwh == pytest.approx(403.0)
    assert cost == pytest.approx(403.0 * ((1 * 0.10 + 3 * 0.30) / 4.0))


async def test_a_range_takes_four_queries_however_many_plugs_and_snapshots(db_session, test_engine):
    day, next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=12), 0.30))
    for index in range(4):
        plug = await _plug(db_session, name=f"plug-{index}")
        db_session.add(
            SmartPlugEnergySnapshot(plug_id=plug.id, recorded_at=_naive(day - timedelta(hours=1)), lifetime_kwh=1.0)
        )
        for hour in range(24):
            db_session.add(
                SmartPlugEnergySnapshot(
                    plug_id=plug.id,
                    recorded_at=_naive(day + timedelta(hours=hour, minutes=30)),
                    lifetime_kwh=1.0 + hour,
                )
            )
    await db_session.commit()
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        kwh, _cost, _warming = await statistics_energy.range_energy_and_cost(db_session, dt_from=day, dt_to=next_day)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)

    assert kwh == pytest.approx(4 * 23.0)
    assert len(statements) == 4, statements  # plugs, price history, baselines, snapshots in the range


def _live(readings: dict):
    """Every plug answers its live counter from ``readings`` (by plug name) -- no device is asked."""

    async def service_for(plug, db):
        return SimpleNamespace(get_energy=AsyncMock(return_value=readings.get(plug.name)))

    return patch.object(statistics_energy.smart_plug_manager, "get_service_for_plug", side_effect=service_for)


async def test_all_time_is_the_history_at_its_own_prices_plus_the_tail_at_the_price_now(db_session):
    day, _next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=15), 0.50))
    plug = await _plug(db_session)
    await _snapshot(db_session, plug.id, day - timedelta(hours=1), 100.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=10), 101.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=20), 103.0)

    with _live({"meter": {"total": 105.0}}):
        kwh, cost = await statistics_energy.live_totals_and_cost(db_session)
        # What the counter held when first seen (100 at 0.10), the steps (1 and 2 at 0.10), and the
        # 2 kWh since the last snapshot at the price now (0.50).
        assert (kwh, cost) == (pytest.approx(105.0), pytest.approx(100 * 0.10 + 3 * 0.10 + 2 * 0.50))

        await _change_price(db_session, 0.90)
        kwh, cost = await statistics_energy.live_totals_and_cost(db_session)

    # Only the tail follows the new price; the past keeps what it cost.
    assert (kwh, cost) == (pytest.approx(105.0), pytest.approx(100 * 0.10 + 3 * 0.10 + 2 * 0.90))


async def test_all_time_costs_a_counter_without_snapshots_at_the_baseline_and_a_daily_figure_at_the_price_now(
    db_session,
):
    day, _next_day = _day()
    await _prices(db_session, 0.10, (day, 0.50))
    await _plug(db_session, name="unseen")
    daily = await _plug(db_session, name="daily")
    daily.plug_type = "rest"
    await db_session.commit()
    await _plug(db_session, name="mute")

    with _live({"unseen": {"total": 40.0}, "daily": {"today": 2.0}, "mute": None}):
        kwh, cost = await statistics_energy.live_totals_and_cost(db_session)
        assert await statistics_energy.sum_live_plug_totals(db_session) == pytest.approx(kwh)

    assert kwh == pytest.approx(42.0)
    assert cost == pytest.approx(40.0 * 0.10 + 2.0 * 0.50)


async def test_all_time_reads_only_the_current_sources_snapshots(db_session):
    day, _next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=12), 0.30))
    plug = await _plug(db_session)
    plug.measurement_source_since = _naive(day + timedelta(hours=11))
    await db_session.commit()
    await _snapshot(db_session, plug.id, day + timedelta(hours=1), 9000.0)  # the meter before
    await _snapshot(db_session, plug.id, day + timedelta(hours=13), 5.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=20), 7.0)

    with _live({"meter": {"total": 7.0}}):
        kwh, cost = await statistics_energy.live_totals_and_cost(db_session)

    # 5 kWh on the counter when this meter was first seen (13:00 -> 0.30), 2 more from that point.
    assert (kwh, cost) == (pytest.approx(7.0), pytest.approx(5 * 0.30 + 2 * 0.30))


async def test_the_overview_answers_with_cost_at_its_own_time(async_client, db_session):
    day, next_day = _day()
    await _prices(db_session, 0.10, (day + timedelta(hours=15), 0.50))
    plug = await _plug(db_session)
    await _snapshot(db_session, plug.id, day - timedelta(hours=1), 100.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=10), 101.0)
    await _snapshot(db_session, plug.id, day + timedelta(hours=20), 103.0)
    await _change_price(db_session, 7.0)

    headers = {"X-Client-Timezone": "UTC"}
    ranged = await async_client.get(
        f"/api/v1/statistics/overview?date_from={day.date()}&date_to={day.date()}", headers=headers
    )
    with _live({"meter": {"total": 103.0}}):
        all_time = await async_client.get("/api/v1/statistics/overview", headers=headers)

    assert ranged.status_code == 200 and all_time.status_code == 200, (ranged.text, all_time.text)
    assert (ranged.json()["total_energy_kwh"], ranged.json()["total_energy_cost"]) == (3.0, 0.3)
    assert (all_time.json()["total_energy_kwh"], all_time.json()["total_energy_cost"]) == (103.0, 10.3)
