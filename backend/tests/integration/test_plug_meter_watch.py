"""A plug's stalled lifetime counter, end to end (spec plug-meter-watchdog r4; plan A01-A35).

A plug can keep reporting watts while its lifetime counter stands still; per-print energy then read
0 for prints that drew hundreds of watt-hours. These drive the real writers against a real database:
the print's energy (``main._record_print_energy``), the sweep and its one inbox warning, the flush
with its source fence, the power chart with its unusable moments, a configuration change, the
migration and a plug's delete.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot
from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import measurement_buffer as buffer_module
from backend.app.services.measurement_buffer import BATCH_UNKNOWN, INVALID_INPUT, measurement_buffer
from backend.app.services.measurement_history import flush_buffered
from backend.app.services.meter_watch import EVENT, MeterWatch

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TOKEN = "a" * 32


class _SessionProxy:
    """``async with async_session() as db`` over the test's own session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean_buffer():
    """The buffer is the process's: nothing a test leaves in it may reach the next."""
    measurement_buffer.drain()
    yield
    measurement_buffer.drain()
    measurement_buffer._retired = False


@pytest.fixture
def factory(db_session, monkeypatch):
    proxy = lambda: _SessionProxy(db_session)  # noqa: E731
    monkeypatch.setattr(main, "async_session", proxy)
    return proxy


async def _plug(db, printer_id=None, *, plug_type="mqtt", since: datetime | None = None, **over) -> SmartPlug:
    plug = SmartPlug(
        name=over.pop("name", "meter"),
        plug_type=plug_type,
        enabled=True,
        printer_id=printer_id,
        mqtt_power_topic="tele/meter/SENSOR" if plug_type == "mqtt" else None,
        zigbee_ieee="a4:c1:38:00:00:00:00:09" if plug_type == "zigbee" else None,
        measurement_source_id=TOKEN,
        measurement_source_since=_naive(since or datetime.now(timezone.utc) - timedelta(days=2)),
        **over,
    )
    db.add(plug)
    await db.commit()
    await db.refresh(plug)
    return plug


async def _power(db, plug_id: int, start: datetime, end: datetime, watts: float, *, step_s=60, reason=None):
    moment = start
    while moment <= end:
        db.add(SmartPlugPowerHistory(plug_id=plug_id, power=watts, recorded_at=_naive(moment), unusable_reason=reason))
        moment += timedelta(seconds=step_s)
    await db.commit()


async def _snapshot(db, plug_id: int, at: datetime, kwh: float):
    db.add(SmartPlugEnergySnapshot(plug_id=plug_id, recorded_at=_naive(at), lifetime_kwh=kwh))
    await db.commit()


async def _archive(db, printer_id: int, plug: SmartPlug, start: datetime, end: datetime, start_kwh: float, **extra):
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
            "energy_source_id": extra.pop("source", plug.measurement_source_id),
            "energy_start_at": start.isoformat(),
            **extra,
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


async def _user(db, name="operator") -> User:
    user = User(username=name, password_hash="x", role="user", is_active=True)
    db.add(user)
    await db.commit()
    return user


async def _inbox_rows(db) -> int:
    return await db.scalar(
        select(func.count()).select_from(UserNotification).where(UserNotification.event_type == EVENT)
    )


async def _print(db, printer_factory, *, minutes=60, plug_type="mqtt", **plug_over):
    printer = await printer_factory()
    plug = await _plug(db, printer.id, plug_type=plug_type, **plug_over)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    start = end - timedelta(minutes=minutes)
    return printer, plug, start, end


# ---- a print's energy (A01-A05, A10, A13, A24, A25) ------------------------------------------


async def test_a01_a_flat_counter_under_200_w_for_an_hour_is_estimated_from_the_power(
    db_session, printer_factory, factory
):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _user(db_session)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2, abs=1e-3)
    assert archive.energy_cost is not None
    extra = archive.extra_data
    assert extra["energy_is_approximate"] is True
    assert extra["energy_reconstructed_from"] == "power_history"
    record = extra["energy_provenance"]
    assert record["kind"] == "estimated" and record["final"] is True
    assert record["reason"] == "meter_stalled" and record["counter_delta_kwh"] == 0.0
    assert record["coverage"] >= 0.95
    # The stall is an episode now, and its one warning reached the inbox.
    incident = (await db_session.execute(select(SmartPlugMeterIncident))).scalar_one()
    assert incident.status == "active" and incident.notified is True
    assert record["incident_id"] == incident.id
    assert await _inbox_rows(db_session) == 1


async def test_a02_a_short_light_print_with_no_counter_step_stays_a_measured_zero(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory, minutes=3)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 120.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == 0.0
    assert "energy_provenance" not in archive.extra_data
    assert "energy_is_approximate" not in archive.extra_data
    assert (await db_session.execute(select(SmartPlugMeterIncident))).first() is None


async def test_a03_a_healthy_counter_stays_the_measurement(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.1)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.19):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.19)
    assert "energy_provenance" not in archive.extra_data


async def test_a03_an_episode_the_counter_left_before_the_print_does_not_mark_it(db_session, printer_factory, factory):
    """The sweep has not closed it yet, but the counter rose between the stall and this print."""
    printer, plug, start, end = await _print(db_session, printer_factory)
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(start - timedelta(hours=3)),
            flat_since=_naive(start - timedelta(hours=4)),
            flat_until=_naive(start - timedelta(hours=3)),
            total_kwh=9.0,
            notified=True,
        )
    )
    await db_session.commit()
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start - timedelta(hours=1), 9.5)
    await _snapshot(db_session, plug.id, start, 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.2):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2)
    assert "energy_provenance" not in archive.extra_data


async def test_a04_a_flat_tail_after_growth_estimates_the_whole_window_once(db_session, printer_factory, factory):
    """The counter caught 50 Wh of the first half hour and nothing after: the window is 400 Wh."""
    printer, plug, start, end = await _print(db_session, printer_factory, minutes=90)
    half = start + timedelta(minutes=30)
    await _power(db_session, plug.id, start - timedelta(minutes=1), half, 400.0)
    await _power(db_session, plug.id, half + timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, half, 10.05)
    await _snapshot(db_session, plug.id, half + timedelta(minutes=40), 10.05)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.05):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    # Not 0.05 (the counter), not 0.05 + the tail: the window's own integral, once.
    assert archive.energy_kwh == pytest.approx(0.4, abs=0.01)
    assert archive.extra_data["energy_provenance"]["kind"] == "estimated"


async def test_a05_an_episode_with_a_hole_in_the_history_is_unknown_not_zero(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(start - timedelta(hours=1)),
            flat_since=_naive(start - timedelta(hours=2)),
            flat_until=_naive(start - timedelta(hours=1)),
            total_kwh=10.0,
            notified=True,
        )
    )
    await db_session.commit()
    # Twenty minutes of readings, then nothing: forty minutes of the window are unknown.
    await _power(db_session, plug.id, start - timedelta(minutes=1), start + timedelta(minutes=20), 200.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None and archive.energy_cost is None
    record = archive.extra_data["energy_provenance"]
    assert record["kind"] == "unavailable" and record["unavailable_reason"] == "insufficient_history"
    assert record["final"] is True


async def test_a05_unusable_moments_are_not_bridged_into_coverage(db_session, printer_factory, factory):
    """A30: valid 200 W readings 170 s apart, a refused one between each pair.

    Dropping the refused readings would bridge every pair (170 s is inside Zigbee's 180 s limit) and
    call the window fully covered; kept as markers, they leave no usable segment at all.
    """
    printer, plug, start, end = await _print(db_session, printer_factory, plug_type="zigbee")
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(start),
            flat_since=_naive(start - timedelta(hours=1)),
            flat_until=_naive(start),
            total_kwh=10.0,
            notified=True,
        )
    )
    await db_session.commit()
    first = start - timedelta(seconds=170)
    await _power(db_session, plug.id, first, end + timedelta(seconds=170), 200.0, step_s=170)
    await _power(
        db_session,
        plug.id,
        first + timedelta(seconds=85),
        end + timedelta(seconds=85),
        0.0,
        step_s=170,
        reason="state_unknown",
    )
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None
    assert archive.extra_data["energy_provenance"]["unavailable_reason"] == "insufficient_history"


async def test_a10_an_archive_from_before_the_watch_keeps_the_old_rule(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    archive = PrintArchive(
        printer_id=printer.id,
        filename="a.3mf",
        file_path="x/a.3mf",
        file_size=1,
        status="completed",
        completed_at=_naive(end),
        energy_start_kwh=10.0,
        extra_data={"energy_plug_id": plug.id},
    )
    db_session.add(archive)
    await db_session.commit()

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == 0.0
    assert "energy_provenance" not in archive.extra_data


async def test_a10_a_final_figure_is_never_overwritten(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)
    await db_session.refresh(archive)
    first = (archive.energy_kwh, dict(archive.extra_data))

    # A second callback with another reading changes nothing.
    with _end_reading(10.5):
        await main._record_print_energy(archive.id, printer.id)
    await db_session.refresh(archive)
    assert (archive.energy_kwh, archive.extra_data) == first


async def test_a13_the_buffered_tail_counts_at_once(db_session, printer_factory, factory):
    """The last half hour is still in memory, not yet flushed: the estimate reads it there."""
    printer, plug, start, end = await _print(db_session, printer_factory)
    half = start + timedelta(minutes=30)
    await _power(db_session, plug.id, start - timedelta(minutes=1), half, 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, half, 10.0)
    moment = half + timedelta(minutes=1)
    with measurement_buffer._lock:
        while moment <= end + timedelta(minutes=1):
            measurement_buffer._power.append(buffer_module.PowerSample(plug.id, 200.0, moment, TOKEN, None))
            moment += timedelta(seconds=60)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2, abs=1e-3)
    # Copied, never consumed: the flush still owns them.
    assert measurement_buffer.pending() > 0


async def test_a24_a_source_changed_under_the_print_is_unknown_not_a_delta(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0, source="b" * 32)

    with _end_reading(10.3):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None
    record = archive.extra_data["energy_provenance"]
    assert record["unavailable_reason"] == "source_changed" and record["final"] is True


async def test_a25_an_unknown_end_over_a_stall_is_unknown_not_an_approximate_zero(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id, approximate=True)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None
    assert archive.extra_data["energy_provenance"]["unavailable_reason"] == "unknown_end"


async def test_a25_an_unknown_end_over_a_healthy_counter_keeps_the_approximate_delta(
    db_session, printer_factory, factory
):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _snapshot(db_session, plug.id, start, 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.4):
        await main._record_print_energy(archive.id, printer.id, approximate=True)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.4)
    assert archive.extra_data["energy_is_approximate"] is True
    assert "energy_provenance" not in archive.extra_data


async def test_restore_retires_the_writer(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)
    measurement_buffer.retire()

    with _end_reading(10.3):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None
    assert "energy_provenance" not in archive.extra_data
    # Nor a counter snapshot into the storage that replaced the old one.
    snapshots = await db_session.scalar(select(func.count()).select_from(SmartPlugEnergySnapshot))
    assert snapshots == 0


async def test_restore_retires_the_start_writer(db_session, printer_factory):
    printer = await printer_factory()
    await _plug(db_session, printer.id)
    archive = PrintArchive(printer_id=printer.id, filename="a.3mf", file_path="", file_size=1, status="printing")
    db_session.add(archive)
    await db_session.commit()
    measurement_buffer.retire()

    with _end_reading(10.0):
        assert await main._record_energy_start(archive, printer.id, db_session) is False

    assert await db_session.scalar(select(func.count()).select_from(SmartPlugEnergySnapshot)) == 0


async def test_the_start_is_not_taken_from_a_source_that_changed_under_its_read(db_session, printer_factory):
    printer = await printer_factory()
    await _plug(db_session, printer.id)
    archive = PrintArchive(printer_id=printer.id, filename="a.3mf", file_path="", file_size=1, status="printing")
    db_session.add(archive)
    await db_session.commit()

    async def rotate_during_read(p, db, *, force_read=False):
        await db.execute(
            text("UPDATE smart_plugs SET measurement_source_id = :t WHERE id = :p"), {"t": "c" * 32, "p": p.id}
        )
        return {"total": 10.0}

    with patch.object(main, "_get_plug_energy", side_effect=rotate_during_read):
        assert await main._record_energy_start(archive, printer.id, db_session) is False

    await db_session.refresh(archive)
    assert archive.energy_start_kwh is None


async def test_the_start_records_its_source_and_moment(db_session, printer_factory):
    printer = await printer_factory()
    plug = await _plug(db_session, printer.id)
    archive = PrintArchive(printer_id=printer.id, filename="a.3mf", file_path="", file_size=1, status="printing")
    db_session.add(archive)
    await db_session.commit()

    with _end_reading(10.0):
        assert await main._record_energy_start(archive, printer.id, db_session) is True

    await db_session.refresh(archive)
    assert archive.extra_data["energy_source_id"] == plug.measurement_source_id
    assert datetime.fromisoformat(archive.extra_data["energy_start_at"]).tzinfo is not None


# ---- the sweep and its one warning (A08, A16) -------------------------------------------------


async def test_a08_the_sweep_opens_one_episode_warns_once_and_closes_it_on_new_movement(db_session, factory):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    await _user(db_session)
    await _power(db_session, plug.id, now - timedelta(hours=3), now, 200.0)
    for minutes in (180, 120, 90):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), 10.0)
    watch = MeterWatch()

    assert await watch.sweep(factory, 60, force=True) == 1
    incident = (await db_session.execute(select(SmartPlugMeterIncident))).scalar_one()
    assert incident.status == "active" and incident.notified is True
    assert await _inbox_rows(db_session) == 1
    first_until = incident.flat_until

    # No new counter point: nothing evaluated, nothing repeated.
    assert await watch.sweep(factory, 60, force=True) == 0
    # A new flat point extends the episode without a second warning (a restart is the same: state is durable).
    await _snapshot(db_session, plug.id, now - timedelta(minutes=60), 10.0)
    assert await MeterWatch().sweep(factory, 60, force=True) == 1
    assert await _inbox_rows(db_session) == 1
    assert (await db_session.execute(select(func.count()).select_from(SmartPlugMeterIncident))).scalar() == 1
    await db_session.refresh(incident)
    assert incident.flat_until > first_until

    # The counter moves again: recovered.
    await _snapshot(db_session, plug.id, now - timedelta(minutes=30), 10.3)
    assert await watch.sweep(factory, 60, force=True) == 1
    await db_session.refresh(incident)
    assert incident.status == "closed" and incident.close_reason == "recovered"
    assert await _inbox_rows(db_session) == 1


async def test_a16_an_episode_with_nobody_subscribed_is_still_recorded(db_session, factory):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    await _power(db_session, plug.id, now - timedelta(hours=2), now, 200.0)
    for minutes in (120, 60, 0):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), 10.0)

    assert await MeterWatch().sweep(factory, 60, force=True) == 1

    incident = (await db_session.execute(select(SmartPlugMeterIncident))).scalar_one()
    assert incident.status == "active"
    assert await _inbox_rows(db_session) == 0


async def test_a07_a_counter_reset_is_neither_a_stall_nor_a_recovery(db_session, factory):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    await _power(db_session, plug.id, now - timedelta(hours=3), now, 200.0)
    for minutes, total in ((180, 10.0), (120, 10.0), (60, 0.0), (0, 0.2)):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), total)
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(now - timedelta(hours=2)),
            flat_since=_naive(now - timedelta(hours=3)),
            flat_until=_naive(now - timedelta(hours=2)),
            total_kwh=10.0,
            notified=True,
        )
    )
    await db_session.commit()

    await MeterWatch().sweep(factory, 60, force=True)

    incident = (await db_session.execute(select(SmartPlugMeterIncident))).scalar_one()
    # The fall to 0.0 is a reset; the rise from 0.0 to 0.2 is the counter moving again AFTER the episode.
    assert incident.status == "closed" and incident.close_reason == "recovered"


async def test_meter_incidents_are_listed_for_the_plug_page(async_client: AsyncClient, db_session):
    plug = await _plug(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(now),
            flat_since=_naive(now - timedelta(hours=1)),
            flat_until=_naive(now),
            total_kwh=10.0,
            evidence={"integral_wh": 200.0},
            notified=True,
        )
    )
    await db_session.commit()

    response = await async_client.get("/api/v1/smart-plugs/meter-incidents")

    assert response.status_code == 200, response.text
    [row] = response.json()
    assert row["plug_id"] == plug.id and row["evidence"]["integral_wh"] == 200.0


# ---- the flush and its source fence (A11, A12, A23, A35) ---------------------------------------


async def test_a11_a_reading_of_another_source_or_before_the_cutoff_is_dropped(db_session):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session, since=now - timedelta(minutes=10))
    measurement_buffer.record_power(plug.id, 50.0, source_token=TOKEN)
    measurement_buffer.record_power(plug.id, 60.0, source_token="z" * 32)
    with measurement_buffer._lock:
        measurement_buffer._power.append(
            buffer_module.PowerSample(plug.id, 70.0, now - timedelta(minutes=20), TOKEN, None)
        )
        # The very instant of the cutoff belongs to the source before.
        measurement_buffer._power.append(
            buffer_module.PowerSample(plug.id, 80.0, now - timedelta(minutes=10), TOKEN, None)
        )

    assert await flush_buffered(db_session) == 1

    rows = (await db_session.execute(select(SmartPlugPowerHistory.power))).scalars().all()
    assert rows == [50.0]


async def test_a12_a_failed_write_puts_the_batch_back(db_session, monkeypatch):
    plug_id = (await _plug(db_session)).id  # the rollback below expires the row
    measurement_buffer.record_power(plug_id, 50.0, source_token=TOKEN)

    async def fail(db, power):
        raise RuntimeError("database gone")

    from backend.app.services import measurement_history

    monkeypatch.setattr(measurement_history, "_write_power", fail)
    with pytest.raises(RuntimeError):
        await flush_buffered(db_session)
    await db_session.rollback()
    monkeypatch.undo()

    samples, _ = measurement_buffer.peek_power(plug_id)
    assert [(s.power, s.reason) for s in samples] == [(50.0, None)]
    assert await flush_buffered(db_session) == 1


async def test_a35_a_commit_of_unknown_outcome_leaves_the_moments_unusable(db_session, monkeypatch):
    plug = await _plug(db_session)
    measurement_buffer.record_power(plug.id, 50.0, source_token=TOKEN)
    real_commit = db_session.commit

    async def lost_answer():
        await real_commit()  # it did land...
        raise ConnectionError("...but the answer did not")

    monkeypatch.setattr(db_session, "commit", lost_answer)
    with pytest.raises(ConnectionError):
        await flush_buffered(db_session)
    monkeypatch.setattr(db_session, "commit", real_commit)

    samples, _ = measurement_buffer.peek_power(plug.id)
    assert [s.reason for s in samples] == [BATCH_UNKNOWN]
    assert await flush_buffered(db_session) == 1
    rows = (await db_session.execute(select(SmartPlugPowerHistory.power, SmartPlugPowerHistory.unusable_reason))).all()
    # The reading and its marker share the moment: the moment is unusable either way.
    assert sorted(rows, key=lambda r: r[1] or "") == [(50.0, None), (0.0, BATCH_UNKNOWN)]


async def test_an_invalid_value_is_kept_as_a_hole(db_session):
    plug = await _plug(db_session)
    measurement_buffer.record_power(plug.id, "nan", source_token=TOKEN)

    assert await flush_buffered(db_session) == 1

    row = (await db_session.execute(select(SmartPlugPowerHistory))).scalar_one()
    assert (row.power, row.unusable_reason) == (0.0, INVALID_INPUT)


async def test_a23_a_configuration_change_rotates_the_source_and_closes_the_episode(
    async_client: AsyncClient, db_session
):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session, plug_type="tasmota", ip_address="10.0.0.5", name="before")
    # A row stamped later than now (a clock that went back) must still fall before the new cutoff.
    future = now + timedelta(minutes=5)
    db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=1.0, recorded_at=_naive(future)))
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="active",
            opened_at=_naive(now),
            flat_since=_naive(now - timedelta(hours=1)),
            flat_until=_naive(now),
            total_kwh=10.0,
            notified=True,
        )
    )
    await db_session.commit()

    renamed = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"name": "after"})
    assert renamed.status_code == 200, renamed.text
    await db_session.refresh(plug)
    assert plug.measurement_source_id == TOKEN  # a rename is the same meter

    moved = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"ip_address": "10.0.0.6"})
    assert moved.status_code == 200, moved.text
    await db_session.commit()
    await db_session.refresh(plug)
    assert plug.measurement_source_id != TOKEN
    assert plug.measurement_source_since >= _naive(future)
    incident = (await db_session.execute(select(SmartPlugMeterIncident))).scalar_one()
    await db_session.refresh(incident)
    assert incident.status == "closed" and incident.close_reason == "source_changed"


async def test_deleting_a_plug_removes_its_episodes(async_client: AsyncClient, db_session):
    plug = await _plug(db_session, plug_type="tasmota", ip_address="10.0.0.7")
    now = datetime.now(timezone.utc)
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=TOKEN,
            status="closed",
            opened_at=_naive(now),
            flat_since=_naive(now),
            flat_until=_naive(now),
            total_kwh=1.0,
            notified=True,
        )
    )
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/smart-plugs/{plug.id}")

    assert response.status_code == 200, response.text
    await db_session.commit()
    assert (await db_session.execute(select(SmartPlugMeterIncident))).first() is None


# ---- the chart (A33, A34) --------------------------------------------------------------------


def _bucket_start(hours_ago: float, bucket_seconds: int = 300) -> datetime:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    epoch = moment.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % bucket_seconds), tz=timezone.utc)


async def test_a33_a_bucket_holding_a_marker_is_a_hole_and_the_stats_skip_its_moment(
    async_client: AsyncClient, db_session
):
    plug = await _plug(db_session, plug_type="tasmota", ip_address="10.0.0.8")
    base = _bucket_start(3)
    rows = [
        (base + timedelta(seconds=10), 100.0, None),
        (base + timedelta(minutes=5, seconds=10), 900.0, None),  # a valid duplicate of a marked moment
        (base + timedelta(minutes=5, seconds=10), 0.0, "state_unknown"),
        (base + timedelta(minutes=5, seconds=40), 200.0, None),
        (base + timedelta(minutes=10, seconds=10), 300.0, None),
    ]
    for at, power, reason in rows:
        db_session.add(
            SmartPlugPowerHistory(plug_id=plug.id, power=power, recorded_at=_naive(at), unusable_reason=reason)
        )
    await db_session.commit()

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()

    assert [p["power"] for p in body["points"]] == [100.0, None, 300.0]
    assert (body["min_power"], body["max_power"]) == (100.0, 300.0)


async def test_a34_a_marker_only_edge_still_shows_as_a_hole(async_client: AsyncClient, db_session):
    plug = await _plug(db_session, plug_type="tasmota", ip_address="10.0.0.9")
    base = _bucket_start(3)
    db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=100.0, recorded_at=_naive(base + timedelta(seconds=5))))
    db_session.add(
        SmartPlugPowerHistory(
            plug_id=plug.id,
            power=0.0,
            recorded_at=_naive(base + timedelta(minutes=10, seconds=5)),
            unusable_reason="buffer_loss",
        )
    )
    await db_session.commit()

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()

    assert [p["power"] for p in body["points"]] == [100.0, None, None]
    assert (body["min_power"], body["avg_power"], body["max_power"]) == (100.0, 100.0, 100.0)


# ---- the migration (A32) ---------------------------------------------------------------------


async def test_a32_the_migration_seeds_a_source_after_every_old_row(tmp_path):
    from backend.app.migrations import m211_plug_meter_watch as m211

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm211.db'}")
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE smart_plugs (id INTEGER PRIMARY KEY, name VARCHAR(100))"))
            await conn.execute(
                text(
                    "CREATE TABLE smart_plug_power_history (id INTEGER PRIMARY KEY, plug_id INTEGER, "
                    "power FLOAT, recorded_at DATETIME)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE smart_plug_energy_snapshots (id INTEGER PRIMARY KEY, plug_id INTEGER, "
                    "lifetime_kwh FLOAT, recorded_at DATETIME)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE notification_templates (id INTEGER PRIMARY KEY, event_type VARCHAR(50) UNIQUE, "
                    "name VARCHAR(100), title_template TEXT, body_template TEXT, is_default BOOLEAN, "
                    "created_at DATETIME, updated_at DATETIME)"
                )
            )
            await conn.execute(text("CREATE TABLE settings (id INTEGER PRIMARY KEY, key VARCHAR(100), value TEXT)"))
            await conn.execute(text("INSERT INTO smart_plugs (id, name) VALUES (1, 'a'), (2, 'b')"))
            await conn.execute(
                text("INSERT INTO smart_plug_power_history (plug_id, power, recorded_at) VALUES (1, 5.0, :at)"),
                {"at": future},
            )
            await m211.upgrade(conn)
            await m211.upgrade(conn)  # safe to rerun

        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await m211.seed(sessions)
        async with engine.begin() as conn:
            plugs = (
                await conn.execute(text("SELECT id, measurement_source_id, measurement_source_since FROM smart_plugs"))
            ).all()
            old = (await conn.execute(text("SELECT power, unusable_reason FROM smart_plug_power_history"))).all()
        first_tokens = {row[0]: row[1] for row in plugs}
        assert all(len(token) == 32 for token in first_tokens.values())
        assert first_tokens[1] != first_tokens[2]
        since = {row[0]: datetime.fromisoformat(str(row[2])) for row in plugs}
        assert since[1] >= future  # strictly after nothing old: the old row is at or before the cutoff
        assert old == [(5.0, None)]

        await m211.seed(sessions)  # a rerun keeps the tokens and adds no second template
        async with engine.begin() as conn:
            again = {
                r[0]: r[1] for r in (await conn.execute(text("SELECT id, measurement_source_id FROM smart_plugs")))
            }
            templates = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM notification_templates WHERE event_type = :e"), {"e": EVENT}
                )
            ).scalar()
        assert again == first_tokens
        assert templates == 1
    finally:
        await engine.dispose()


# ---- what the readers are told (A17) ---------------------------------------------------------


async def _energy_archive(db, kwh, provenance=None, **extra):
    data = dict(extra)
    if provenance is not None:
        data["energy_provenance"] = provenance
    archive = PrintArchive(
        filename="part.3mf",
        file_path="",
        file_size=0,
        status="completed",
        quantity=1,
        energy_kwh=kwh,
        energy_cost=None if kwh is None else kwh * 0.2,
        extra_data=data,
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def test_a17_an_archive_says_what_its_energy_figure_is(async_client: AsyncClient, db_session):
    estimated = await _energy_archive(
        db_session, 0.2, {"kind": "estimated", "coverage": 0.98, "final": True}, energy_is_approximate=True
    )
    unknown = await _energy_archive(
        db_session, None, {"kind": "unavailable", "unavailable_reason": "insufficient_history", "final": True}
    )
    recovered = await _energy_archive(db_session, 0.4, energy_is_approximate=True)
    measured = await _energy_archive(db_session, 0.1)
    none = await _energy_archive(db_session, None)

    async def fields(archive):
        body = (await async_client.get(f"/api/v1/archives/{archive.id}")).json()
        return body["energy_status"], body["energy_coverage"], body["energy_reason"]

    assert await fields(estimated) == ("estimated", 0.98, "meter_stalled")
    assert await fields(unknown) == ("unavailable", None, "insufficient_history")
    assert await fields(recovered) == ("approximate", None, None)
    assert await fields(measured) == ("measured", None, None)
    assert await fields(none) == (None, None, None)


async def test_a17_the_statistics_count_estimated_and_unknown_prints(async_client: AsyncClient, db_session):
    await _energy_archive(db_session, 0.2, {"kind": "estimated", "coverage": 0.98, "final": True})
    await _energy_archive(db_session, None, {"kind": "unavailable", "unavailable_reason": "unknown_end"})
    await _energy_archive(db_session, 0.1)

    totals = (await async_client.get("/api/v1/statistics/aggregate")).json()["totals"]
    overview = (await async_client.get("/api/v1/statistics/overview")).json()

    assert totals["energy_kwh"] == pytest.approx(0.3)
    assert (totals["energy_estimated"], totals["energy_unavailable"]) == (1, 1)
    assert overview["print_energy_kwh"] == pytest.approx(0.3)
    assert (overview["print_energy_estimated"], overview["print_energy_unavailable"]) == (1, 1)


# ---- review r1: recovery, a failed episode write, a late rotation, a refused flush, a policy change ----


async def _active_incident(db, plug, *, flat_since, flat_until, total=10.0):
    incident = SmartPlugMeterIncident(
        plug_id=plug.id,
        source_id=TOKEN,
        status="active",
        opened_at=_naive(flat_until),
        flat_since=_naive(flat_since),
        flat_until=_naive(flat_until),
        total_kwh=total,
        notified=True,
    )
    db.add(incident)
    await db.commit()
    return incident


async def test_an_episode_closes_on_the_next_rise_even_when_the_window_no_longer_proves_the_stall(db_session, factory):
    """W6: idle overnight is no suspicious run, yet the morning's rise is the recovery."""
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    incident = await _active_incident(
        db_session, plug, flat_since=now - timedelta(hours=30), flat_until=now - timedelta(hours=20)
    )
    await _power(db_session, plug.id, now - timedelta(hours=3), now, 5.0, step_s=120)  # standby
    for minutes, total in ((180, 10.0), (120, 10.0), (60, 10.0), (10, 10.02)):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), total)

    assert await MeterWatch().sweep(factory, 60, force=True) == 1

    await db_session.refresh(incident)
    assert incident.status == "closed" and incident.close_reason == "recovered"


async def test_a_reset_then_a_rise_is_a_recovery(db_session, factory):
    """The recommended unplug restarts the counter lower; its first step after that is the counter moving."""
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    incident = await _active_incident(
        db_session, plug, flat_since=now - timedelta(hours=6), flat_until=now - timedelta(hours=4)
    )
    for minutes, total in ((120, 9.8), (60, 9.8), (10, 9.85)):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), total)

    await MeterWatch().sweep(factory, 60, force=True)

    await db_session.refresh(incident)
    assert incident.status == "closed" and incident.close_reason == "recovered"


async def test_a_reset_alone_is_no_recovery(db_session, factory):
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    incident = await _active_incident(
        db_session, plug, flat_since=now - timedelta(hours=6), flat_until=now - timedelta(hours=4)
    )
    for minutes, total in ((120, 9.8), (10, 9.8)):
        await _snapshot(db_session, plug.id, now - timedelta(minutes=minutes), total)

    await MeterWatch().sweep(factory, 60, force=True)

    await db_session.refresh(incident)
    assert incident.status == "active"


async def test_a_print_after_a_reset_and_a_rise_is_measured_again(db_session, printer_factory, factory):
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _active_incident(
        db_session, plug, flat_since=start - timedelta(hours=6), flat_until=start - timedelta(hours=4)
    )
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start - timedelta(hours=2), 9.5)
    await _snapshot(db_session, plug.id, start, 9.6)
    archive = await _archive(db_session, printer.id, plug, start, end, 9.6)

    with _end_reading(9.8):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2)
    assert "energy_provenance" not in archive.extra_data


async def test_a_failed_episode_write_does_not_cost_the_print_its_figure(
    db_session, printer_factory, factory, monkeypatch
):
    from backend.app.services import meter_watch as watch

    async def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(watch, "open_incident_from_print", broken)
    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)

    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == pytest.approx(0.2, abs=1e-3)
    assert archive.extra_data["energy_provenance"]["kind"] == "estimated"


async def test_a_racing_episode_is_adopted_not_doubled(db_session, factory, monkeypatch):
    """The sweep and a print's energy both find the stall: the loser answers with the winner's episode."""
    from backend.app.services import meter_watch as watch
    from backend.app.services.meter_detector import Detection, FlatSeries
    from backend.app.services.power_integrator import Integration

    now = datetime.now(timezone.utc)
    plug = await _plug(db_session)
    real_apply = watch.MeterWatch.apply

    async def racing_apply(self, db, plug_, detection, incident, now_, counters=()):
        await db.execute(
            text(
                "INSERT INTO smart_plug_meter_incidents (plug_id, source_id, status, opened_at, flat_since, "
                "flat_until, total_kwh, notified) VALUES (:p, :s, 'active', :t, :t, :t, 10.0, :n)"
            ),
            {"p": plug_.id, "s": TOKEN, "t": _naive(now_), "n": True},
        )
        await db.commit()
        await real_apply(self, db, plug_, detection, incident, now_, counters)

    monkeypatch.setattr(watch.MeterWatch, "apply", racing_apply)
    integral = Integration(0.2, 3600, 3600, 60, 60, 0, 0, ())
    series = FlatSeries(now - timedelta(hours=1), now, 10.0, integral)

    winner = await watch.open_incident_from_print(factory, plug, Detection("suspicious", [series]), now)

    rows = (await db_session.execute(select(SmartPlugMeterIncident))).scalars().all()
    assert len(rows) == 1 and winner == rows[0].id


async def test_a_rotation_after_the_end_read_is_unknown_not_a_measured_zero(
    db_session, printer_factory, factory, monkeypatch
):
    from backend.app.api.routes import settings as settings_routes

    printer, plug, start, end = await _print(db_session, printer_factory)
    await _power(db_session, plug.id, start - timedelta(minutes=1), end + timedelta(minutes=1), 200.0)
    await _snapshot(db_session, plug.id, start, 10.0)
    await _snapshot(db_session, plug.id, start + timedelta(minutes=30), 10.0)
    archive = await _archive(db_session, printer.id, plug, start, end, 10.0)
    real_get_setting = settings_routes.get_setting

    async def rotate_then_read(db, key):
        # An operator saves a new topic between the end read's check and the assessment.
        await db.execute(
            text("UPDATE smart_plugs SET measurement_source_id = :t, measurement_source_since = :s WHERE id = :p"),
            {"t": "d" * 32, "s": _naive(datetime.now(timezone.utc)), "p": plug.id},
        )
        await db.commit()
        return await real_get_setting(db, key)

    monkeypatch.setattr(settings_routes, "get_setting", rotate_then_read)
    with _end_reading(10.0):
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None
    assert archive.extra_data["energy_provenance"]["unavailable_reason"] == "source_changed"


async def test_a_refused_insert_puts_the_batch_back_unmarked(db_session, monkeypatch):
    """The rows are written before the commit: a refusal there is a known rollback, not an unknown outcome."""
    from sqlalchemy.exc import OperationalError

    plug_id = (await _plug(db_session)).id
    measurement_buffer.record_power(plug_id, 50.0, source_token=TOKEN)

    async def locked(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(db_session, "flush", locked)
    with pytest.raises(OperationalError):
        await flush_buffered(db_session)
    monkeypatch.undo()
    await db_session.rollback()

    samples, _ = measurement_buffer.peek_power(plug_id)
    assert [(s.power, s.reason) for s in samples] == [(50.0, None)]


async def test_a_new_threshold_or_interval_re_evaluates_the_plug(async_client: AsyncClient, db_session):
    """Spec r4: a policy change keeps the source but resets the scan cursor."""
    now = datetime.now(timezone.utc)
    plug = await _plug(db_session, plug_type="tasmota", ip_address="10.0.0.11", meter_watch_cursor=_naive(now))

    response = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"meter_min_expected_wh": 250})

    assert response.status_code == 200, response.text
    await db_session.commit()
    await db_session.refresh(plug)
    assert plug.meter_watch_cursor is None
    assert plug.measurement_source_id == TOKEN
