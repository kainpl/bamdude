"""Plug meter watch: suspected stalls of a lifetime counter, and the marked energy estimate (spec
plug-meter-watchdog r4, W5-W10).

Above the drivers, like the history it reads: nothing here asks a plug anything. Two entry points use
one evaluator (``meter_detector`` over ``power_integrator``):

* the **sweep**, from the measurement-history loop after its flush, at most every
  ``SWEEP_INTERVAL``: per plug, when a new counter point arrived, it looks at the last day and opens,
  extends or closes a stall episode -- one inbox warning per episode, never per print or tick;
* a **print's energy** (``assess_print_window``), from the background energy writer: when the print's
  window meets a proven flat series, the counter's figure is not believed. The whole measured window
  is estimated once from the power history of the same source -- or, without enough history, left
  unknown with its reason. A healthy counter stays the measurement, a short quantized zero included.

Reading is memory first, then a fresh database read (W10): a reading is always in the buffer copy,
in the database, or in both -- never in neither -- and both sides are brought to UTC before they meet.
Everything is restricted to the plug's current measurement source: its token for buffered readings,
its strict cutoff for rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.services.measurement_buffer import measurement_buffer
from backend.app.services.meter_detector import CounterPoint, Detection, detect
from backend.app.services.meter_source import Cadence, cadence_of, min_expected_wh, naive_utc
from backend.app.services.power_integrator import Integration, Sample, as_utc, integrate, normalize

logger = logging.getLogger(__name__)

#: How far back a flat series is looked for -- the sweep's window, and before a print's start.
LOOKBACK = timedelta(hours=24)
#: The sweep runs at most this often (W5).
SWEEP_INTERVAL = 300.0
#: Power rows one evaluation may read before it gives up as ``insufficient_budget`` (W12).
MAX_POWER_ROWS = 200_000
#: Rows per keyset chunk.
CHUNK_ROWS = 2_000

EVENT = "plug_meter_stalled"


class BudgetExceeded(Exception):
    """More power history than one evaluation may read."""


@dataclass
class History:
    power: list
    counters: list[CounterPoint]
    storage_replaced: bool = False


async def _power_rows(db, plug, start: datetime, end: datetime) -> list[Sample]:
    from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

    conditions = [
        SmartPlugPowerHistory.plug_id == plug.id,
        SmartPlugPowerHistory.recorded_at >= naive_utc(start),
        SmartPlugPowerHistory.recorded_at <= naive_utc(end),
    ]
    if plug.measurement_source_since is not None:
        # Strictly after the cutoff (W2): a row of the very instant belongs to the source before.
        conditions.append(SmartPlugPowerHistory.recorded_at > plug.measurement_source_since)
    samples: list[Sample] = []
    last_id = 0
    while True:
        query = (
            select(
                SmartPlugPowerHistory.id,
                SmartPlugPowerHistory.recorded_at,
                SmartPlugPowerHistory.power,
                SmartPlugPowerHistory.unusable_reason,
            )
            .where(*conditions, SmartPlugPowerHistory.id > last_id)
            .order_by(SmartPlugPowerHistory.id)
            .limit(CHUNK_ROWS)
        )
        rows = (await db.execute(query)).all()
        for row in rows:
            samples.append(Sample(row.recorded_at, row.power, row.unusable_reason))
        if len(samples) > MAX_POWER_ROWS:
            raise BudgetExceeded
        if len(rows) < CHUNK_ROWS:
            return samples
        last_id = rows[-1].id


async def _counter_points(db, plug, start: datetime, end: datetime) -> list[CounterPoint]:
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    since = plug.measurement_source_since
    conditions = [
        SmartPlugEnergySnapshot.plug_id == plug.id,
        SmartPlugEnergySnapshot.recorded_at >= naive_utc(start),
        SmartPlugEnergySnapshot.recorded_at <= naive_utc(end),
    ]
    if since is not None:
        conditions.append(SmartPlugEnergySnapshot.recorded_at > since)
    rows = (
        await db.execute(
            select(SmartPlugEnergySnapshot.recorded_at, SmartPlugEnergySnapshot.lifetime_kwh)
            .where(*conditions)
            .order_by(SmartPlugEnergySnapshot.recorded_at)
        )
    ).all()
    return [CounterPoint(as_utc(r.recorded_at), float(r.lifetime_kwh)) for r in rows if r.lifetime_kwh is not None]


async def read_history(session_factory, plug, start: datetime, end: datetime, cadence: Cadence) -> History:
    """The plug's power and counter history over [start, end], memory first, then a fresh read (W10)."""
    buffered, retired = measurement_buffer.peek_power(plug.id)
    if retired:
        return History([], [], storage_replaced=True)
    lower = as_utc(start) - timedelta(seconds=cadence.gap_seconds)
    upper = as_utc(end) + timedelta(seconds=cadence.gap_seconds)
    since = plug.measurement_source_since
    from_memory = [
        Sample(s.recorded_at, s.power, s.reason)
        for s in buffered
        if s.source_token == plug.measurement_source_id
        and (since is None or naive_utc(s.recorded_at) > since)
        and lower <= as_utc(s.recorded_at) <= upper
    ]
    async with session_factory() as read:
        rows = await _power_rows(read, plug, lower, upper)
        counters = await _counter_points(read, plug, start, end)
    return History(normalize(rows + from_memory), counters)


# ---- a print's energy (W8-W10) --------------------------------------------------


@dataclass
class Assessment:
    #: measured / estimated / unavailable / storage_replaced
    kind: str
    kwh: float | None = None
    reason: str | None = None
    integral: Integration | None = None
    cadence: Cadence | None = None
    incident_id: int | None = None
    detection: Detection | None = None
    extra: dict = field(default_factory=dict)


def _overlaps(series, start: datetime, end: datetime) -> bool:
    return series.start <= end and series.end >= start


async def assess_print_window(
    session_factory,
    plug,
    *,
    start_at: datetime,
    end_at: datetime,
    start_total: float,
    end_total: float,
    end_known: bool,
    sample_seconds: int,
) -> Assessment:
    """Whether the counter's figure for this window may be believed, and what to write if not."""
    cadence = cadence_of(plug, sample_seconds)
    start_at, end_at = as_utc(start_at), as_utc(end_at)
    try:
        history = await read_history(session_factory, plug, start_at - LOOKBACK, end_at, cadence)
    except BudgetExceeded:
        return Assessment("unavailable", reason="insufficient_budget", cadence=cadence)
    if history.storage_replaced:
        return Assessment("storage_replaced", cadence=cadence)
    counters = [*history.counters, CounterPoint(start_at, start_total), CounterPoint(end_at, end_total)]
    detection = detect(
        counters,
        history.power,
        gap_limit=cadence.gap_seconds,
        edge_limit=cadence.edge_seconds,
        min_wh=min_expected_wh(plug),
    )
    incident = await _active_incident_over(session_factory, plug, start_at, end_at)
    if incident is not None and _moved_before(counters, incident, start_at):
        incident = None  # the counter moved again before this print began: the sweep just has not closed it
    suspicious = any(_overlaps(s, start_at, end_at) for s in detection.suspicious) or incident is not None
    if not suspicious:
        return Assessment("measured", kwh=end_total - start_total, cadence=cadence, detection=detection)
    incident_id = incident.id if incident is not None else None
    if not end_known:
        return Assessment(
            "unavailable", reason="unknown_end", cadence=cadence, incident_id=incident_id, detection=detection
        )
    integral = integrate(
        history.power, start_at, end_at, gap_limit=cadence.gap_seconds, edge_limit=cadence.edge_seconds
    )
    if not integral.sufficient:
        return Assessment(
            "unavailable",
            reason="insufficient_history",
            integral=integral,
            cadence=cadence,
            incident_id=incident_id,
            detection=detection,
        )
    return Assessment(
        "estimated", kwh=integral.kwh, integral=integral, cadence=cadence, incident_id=incident_id, detection=detection
    )


async def _active_incident_over(session_factory, plug, start: datetime, end: datetime):
    from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident

    async with session_factory() as read:
        incident = (
            await read.execute(
                select(SmartPlugMeterIncident).where(
                    SmartPlugMeterIncident.plug_id == plug.id,
                    SmartPlugMeterIncident.source_id == (plug.measurement_source_id or ""),
                    SmartPlugMeterIncident.status == "active",
                )
            )
        ).scalar_one_or_none()
    if incident is None or as_utc(incident.flat_since) > end:
        return None
    return incident


def rise_after(counters, incident, until: datetime | None = None) -> datetime | None:
    """The first rise of the counter after the episode's last flat point -- its recovery (W6).

    Read off the counter points alone, never off a suspicious run the current window proves again:
    a night of standby after the stall proves no run, yet the morning's step is the recovery. Each
    point is compared with the one before it, the episode's frozen value first, so a reset (the
    recommended unplug) followed by a step counts and a reset alone does not.
    """
    after = as_utc(incident.flat_until)
    points = sorted(
        (c for c in counters if as_utc(c.at) > after and (until is None or as_utc(c.at) <= until)),
        key=lambda c: as_utc(c.at),
    )
    previous = float(incident.total_kwh)
    for point in points:
        if point.total_kwh > previous + 1e-9:
            return as_utc(point.at)
        previous = point.total_kwh
    return None


def _moved_before(counters: list[CounterPoint], incident, start_at: datetime) -> bool:
    """Whether the counter moved again after the episode and by the print's start.

    A rise inside the print is no such proof: the stretch before it may still be frozen, and the
    window is then estimated or left unknown.
    """
    return rise_after(counters, incident, until=start_at) is not None


def provenance(assessment: Assessment, plug, *, start_at, end_at, counter_delta: float, tariff: float) -> dict:
    """The record an archive keeps of where its energy figure came from (W9)."""
    record: dict = {
        "version": 1,
        "kind": assessment.kind,
        "plug_id": plug.id,
        "source_id": plug.measurement_source_id,
        "start": as_utc(start_at).isoformat(),
        "end": as_utc(end_at).isoformat(),
        "counter_delta_kwh": round(counter_delta, 6),
        "tariff": tariff,
    }
    if assessment.kind == "estimated":
        record.update(source_kind="power_history", reason="meter_stalled")
    if assessment.reason:
        record["unavailable_reason"] = assessment.reason
    if assessment.incident_id is not None:
        record["incident_id"] = assessment.incident_id
    if assessment.cadence is not None:
        record["gap_limit_s"] = assessment.cadence.gap_seconds
        record["expected_interval_s"] = assessment.cadence.expected_seconds
    if assessment.integral is not None:
        integral = assessment.integral
        record.update(
            coverage=round(integral.coverage, 4),
            max_gap_s=round(integral.max_gap, 1),
            samples=integral.samples,
            integral_kwh=round(integral.kwh, 6),
        )
        if integral.reasons:
            record["quality_reasons"] = list(integral.reasons)
    return record


def energy_wire_fields(extra_data: dict | None, energy_kwh: float | None) -> dict:
    """What an archive's energy figure is, for its readers (W11) -- never derived from the number alone.

    ``estimated``: integrated from the power history over a stalled counter; ``unavailable``: no usable
    figure, with the reason; ``approximate``: a counter difference over a window that is not exactly the
    print's (a recovered start or end); ``measured``: the counter's difference. None: no energy at all.
    """
    extra = extra_data if isinstance(extra_data, dict) else {}
    record = extra.get("energy_provenance")
    record = record if isinstance(record, dict) else {}
    kind = record.get("kind")
    if kind == "estimated":
        coverage = record.get("coverage")
        return {
            "energy_status": "estimated",
            "energy_coverage": coverage if isinstance(coverage, int | float) else None,
            "energy_reason": "meter_stalled",
        }
    if kind == "unavailable":
        reason = record.get("unavailable_reason")
        return {"energy_status": "unavailable", "energy_coverage": None, "energy_reason": reason}
    if energy_kwh is None:
        return {"energy_status": None, "energy_coverage": None, "energy_reason": None}
    status = "approximate" if extra.get("energy_is_approximate") else "measured"
    return {"energy_status": status, "energy_coverage": None, "energy_reason": None}


#: The ``extra_data`` keys that say what an archive's energy figure is. They travel with the figure
#: (a Git backup), or an estimate comes back reading as a measurement.
ENERGY_RECORD_KEYS: tuple[str, ...] = (
    "energy_provenance",
    "energy_is_approximate",
    "energy_reconstructed_from",
    "energy_start_partial",
)


def energy_record(extra_data: dict | None) -> dict:
    """The energy keys of an archive's ``extra_data``, for a backup."""
    extra = extra_data if isinstance(extra_data, dict) else {}
    return {key: extra[key] for key in ENERGY_RECORD_KEYS if key in extra}


def with_energy_record(extra_data: dict | None, record) -> dict:
    """``extra_data`` with its energy keys made exactly ``record``'s; anything malformed is left out."""
    extra = {k: v for k, v in (extra_data or {}).items() if k not in ENERGY_RECORD_KEYS}
    record = record if isinstance(record, dict) else {}
    provenance = record.get("energy_provenance")
    if isinstance(provenance, dict) and provenance.get("kind") in ("estimated", "unavailable", "measured"):
        extra["energy_provenance"] = provenance
    for flag in ("energy_is_approximate", "energy_start_partial"):
        if record.get(flag) is True:
            extra[flag] = True
    if record.get("energy_reconstructed_from") == "power_history":
        extra["energy_reconstructed_from"] = "power_history"
    return extra


# ---- the sweep and its episodes (W5-W7) -----------------------------------------


class MeterWatch:
    """The sweep's owner: one per process, run by the measurement-history loop."""

    def __init__(self) -> None:
        self._last_sweep = 0.0

    def due(self) -> bool:
        return time.monotonic() - self._last_sweep >= SWEEP_INTERVAL

    async def sweep(self, session_factory, sample_seconds: int, *, force: bool = False) -> int:
        """Evaluate every enabled plug that has a new counter point. Returns how many were evaluated.

        One plug at a time, each in its own short session: a bad plug, a bad row or a failed
        delivery costs that plug this tick, never the others, the flush or the sensor alerts.
        """
        from backend.app.models.smart_plug import SmartPlug

        if not force and not self.due():
            return 0
        self._last_sweep = time.monotonic()
        async with session_factory() as db:
            plug_ids = (
                (
                    await db.execute(
                        select(SmartPlug.id).where(
                            SmartPlug.enabled.is_(True), SmartPlug.measurement_source_id.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
        evaluated = 0
        for plug_id in plug_ids:
            try:
                if await self._evaluate_plug(session_factory, plug_id, sample_seconds):
                    evaluated += 1
            except Exception as exc:  # noqa: BLE001 — one plug must not cost the others their watch
                logger.warning("Meter watch: plug %s not evaluated: %s", plug_id, exc)
        return evaluated

    async def _evaluate_plug(self, session_factory, plug_id: int, sample_seconds: int) -> bool:
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident

        now = datetime.now(timezone.utc)
        async with session_factory() as db:
            plug = await db.get(SmartPlug, plug_id)
            if plug is None or not plug.measurement_source_id:
                return False
            cursor = plug.meter_watch_cursor
            latest = await _newest_counter_at(db, plug)
            if latest is None or (cursor is not None and naive_utc(latest) <= cursor):
                return False  # no new counter endpoint since the last evaluation
            cadence = cadence_of(plug, sample_seconds)
            try:
                history = await read_history(session_factory, plug, now - LOOKBACK, now, cadence)
            except BudgetExceeded:
                logger.info("Meter watch: plug %s history over budget; nothing concluded", plug_id)
                return False
            if history.storage_replaced:
                return False
            detection = detect(
                history.counters,
                history.power,
                gap_limit=cadence.gap_seconds,
                edge_limit=cadence.edge_seconds,
                min_wh=min_expected_wh(plug),
            )
            incident = (
                await db.execute(
                    select(SmartPlugMeterIncident).where(
                        SmartPlugMeterIncident.plug_id == plug.id, SmartPlugMeterIncident.status == "active"
                    )
                )
            ).scalar_one_or_none()
            await self.apply(db, plug, detection, incident, now, history.counters)
            plug.meter_watch_cursor = naive_utc(latest)
            await db.commit()
        return True

    async def apply(self, db, plug, detection: Detection, incident, now: datetime, counters=()) -> None:
        """Open, extend or close the plug's episode from one detection. The caller commits.

        ``counters`` are the points the detection read: the next rise after the episode closes it
        (``rise_after``), whatever the window proves about the stretch before it.
        """
        from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident

        if incident is not None:
            if incident.source_id != plug.measurement_source_id:
                incident.status, incident.closed_at, incident.close_reason = "closed", naive_utc(now), "source_changed"
                await db.flush()
                incident = None
            else:
                rose = rise_after(counters, incident)
                if rose is None and detection.recovered_at is not None:
                    if as_utc(detection.recovered_at) > as_utc(incident.flat_until):
                        rose = as_utc(detection.recovered_at)
                if rose is None:
                    last = next((s for s in reversed(detection.suspicious) if s.total_kwh == incident.total_kwh), None)
                    if last is not None and as_utc(last.end) > as_utc(incident.flat_until):
                        incident.flat_until = naive_utc(last.end)
                    return
                incident.status, incident.closed_at, incident.close_reason = "closed", naive_utc(now), "recovered"
                await db.flush()  # the closed row leaves the one-active index before a new one may enter it
                # An independent stall after the recovery is a new episode with its own warning (W6).
                if not detection.suspicious or as_utc(detection.suspicious[-1].start) < rose:
                    return
        if detection.state != "suspicious" or not detection.suspicious:
            return
        series = detection.suspicious[-1]
        incident = SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id=plug.measurement_source_id,
            status="active",
            opened_at=naive_utc(now),
            flat_since=naive_utc(series.start),
            flat_until=naive_utc(series.end),
            total_kwh=series.total_kwh,
            evidence={
                "integral_wh": round(series.integral.kwh * 1000, 1),
                "coverage": round(series.integral.coverage, 4),
                "samples": series.integral.samples,
            },
            notified=True,
        )
        db.add(incident)
        await db.flush()
        await _notify(db, plug, incident)


async def _newest_counter_at(db, plug) -> datetime | None:
    from sqlalchemy import func

    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    conditions = [SmartPlugEnergySnapshot.plug_id == plug.id]
    if plug.measurement_source_since is not None:
        conditions.append(SmartPlugEnergySnapshot.recorded_at > plug.measurement_source_since)
    return await db.scalar(select(func.max(SmartPlugEnergySnapshot.recorded_at)).where(*conditions))


async def _notify(db, plug, incident) -> None:
    """One inbox warning for the episode, in the episode's own transaction (W7).

    ``deliver`` commits the caller's session when it has recipients; the caller commits again in any
    case, so the episode and its ``notified`` flag land with the inbox rows or not at all.
    """
    from backend.app.services.notification_service import notification_service

    await notification_service.notify_in_app(
        db,
        EVENT,
        {"plug": plug.name, "since": as_utc(incident.flat_since).strftime("%Y-%m-%d %H:%M UTC")},
        extra_data={"plug_id": plug.id, "incident_id": incident.id},
    )


async def open_incident_from_print(session_factory, plug, detection: Detection, now: datetime) -> int | None:
    """A print's energy proved a stall the sweep has not seen yet: open the episode now (W5)."""
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident

    if not detection.suspicious:
        return None
    # Read once: after a refused flush the session's objects are expired and must not be touched.
    plug_id, source_id = plug.id, plug.measurement_source_id
    async with session_factory() as db:
        fresh = await db.get(SmartPlug, plug_id)
        if fresh is None or fresh.measurement_source_id != source_id:
            return None
        existing = (
            await db.execute(
                select(SmartPlugMeterIncident).where(
                    SmartPlugMeterIncident.plug_id == plug_id, SmartPlugMeterIncident.status == "active"
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        detection = Detection(state="suspicious", suspicious=detection.suspicious)
        try:
            await meter_watch.apply(db, fresh, detection, None, now)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — the sweep may have opened it first; its episode stands
            await db.rollback()
            logger.info("Meter watch: plug %s episode not opened from a print: %s", plug_id, exc)
        # Whoever opened it -- this call, or a sweep that won the one-active index -- owns the episode.
        return (
            await db.execute(
                select(SmartPlugMeterIncident.id).where(
                    SmartPlugMeterIncident.plug_id == plug_id, SmartPlugMeterIncident.status == "active"
                )
            )
        ).scalar_one_or_none()


meter_watch = MeterWatch()
