"""Scheduled AMS drying — the ONE writer of ``drying_schedules`` and ``scheduled_dryings``.

Spec: vault 60-specs/scheduled-drying-spec. Routes and ``PrintScheduler`` call
this module; nothing else writes the two tables. A rule's time is wall-clock in
the SERVER's timezone (``core/timezones.server_timezone()``): what the server does
on its own schedule belongs to the farm, not to whichever browser created it.
"""

from __future__ import annotations

import json
import logging
import time as monotonic_clock
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.timezones import server_timezone
from backend.app.i18n import current_language
from backend.app.models.printer import Printer
from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.services import drying_preflight
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import find_ams_unit, printer_manager
from backend.app.utils.ams_drying import is_drying_active

logger = logging.getLogger(__name__)

UTC = timezone.utc

RUN_ACTIVE = ("pending", "running")
RUN_LISTED = ("pending", "running", "failed", "skipped")
_DATA = Path(__file__).resolve().parents[1] / "data"

RETENTION_DAYS = 7
PRUNE_INTERVAL_SECONDS = 60 * 60
# The printer's report does not show a cycle the instant the command lands.
START_GRACE_SECONDS = 120
# A cycle that ended past this share of its duration did its job — the firmware
# counts down from what it was asked, and a stop in the cooling tail is not a failure.
COMPLETE_FRACTION = 0.9
_BUSY_STATES = frozenset({"RUNNING", "PREPARE", "PAUSE"})

# A cycle the unit never reported running within this long after the command is
# "did not start", not "stopped by hand" — the operator was told it started.
START_CONFIRM_SECONDS = 15 * 60

_last_prune: float | None = None  # monotonic; None = never, so the first pass after a restart prunes
_last_running: set[int] = set()  # printers with a running run at the end of the previous pass
# Runs whose unit has been seen drying. In memory on purpose: after a restart a run
# older than START_CONFIRM_SECONDS is judged as before (stopped early = cancelled).
_seen_active: set[int] = set()


class DryingRefused(Exception):
    """A request the scheduled-drying writer refuses; the routes answer ``status_code`` + the message."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def reason_text(code: str | None, lang: str) -> str:
    """The sentence for a reason code, in ``lang`` (English fallback, then the code itself)."""
    if not code:
        return ""
    for candidate in (lang, "en"):
        path = _DATA / f"drying_reasons_{candidate}.json"
        if path.is_file():
            text = json.loads(path.read_text(encoding="utf-8")).get(code)
            if text:
                return text
    return code


def parse_hhmm(value: str) -> time:
    """ "HH:MM" → ``time``; exactly two digits each, 00:00–23:59."""
    parts = (value or "").split(":")
    if len(parts) != 2 or any(len(p) != 2 or not p.isdigit() for p in parts):
        raise ValueError(f"not an HH:MM time: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"not an HH:MM time: {value!r}")
    return time(hour, minute)


def as_aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_naive_utc(value: datetime) -> datetime:
    return as_aware_utc(value).replace(tzinfo=None)


def _local_instant(day: date, at: time, tz: tzinfo) -> datetime:
    """Wall clock ``at`` on ``day`` as an aware UTC instant.

    ``zoneinfo`` with ``fold=0`` resolves a time the clock skips (spring forward)
    with the offset before the gap — "03:30" becomes 04:30 — and an ambiguous
    one (autumn) as its first occurrence. Both are what a farm expects: the run
    still happens that night, once.
    """
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def next_occurrence(start_time: str, weekdays: int, after: datetime, tz: tzinfo) -> datetime:
    """The first run of a rule strictly after ``after``, as aware UTC."""
    if not weekdays & 0b1111111:
        raise ValueError("weekdays selects no day")
    at = parse_hhmm(start_time)
    after = as_aware_utc(after)
    first_day = after.astimezone(tz).date()
    for offset in range(8):
        day = first_day + timedelta(days=offset)
        if not weekdays & (1 << day.weekday()):
            continue
        candidate = _local_instant(day, at, tz)
        if candidate > after:
            return candidate
    raise ValueError("no occurrence within a week")  # unreachable with a non-empty mask


def window_end(occurrence: datetime, start_time: str, latest_start: str | None, weekdays: int, tz: tzinfo) -> datetime:
    """The last moment a run may start: the rule's "not later than", else the next run."""
    occurrence = as_aware_utc(occurrence)
    if latest_start is None:
        return next_occurrence(start_time, weekdays, occurrence, tz)
    start_day = occurrence.astimezone(tz).date()
    latest = parse_hhmm(latest_start)
    end_day = start_day if latest > parse_hhmm(start_time) else start_day + timedelta(days=1)
    return _local_instant(end_day, latest, tz)


# ---------------------------------------------------------------- the writer


async def validate_target(db: AsyncSession, printer_id: int, ams_id: int, temp: int, duration_hours: int) -> Printer:
    """The checks the manual drying button makes, answered as a refusal (spec §API)."""
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise DryingRefused("Printer not found", 404)
    state = printer_manager.get_status(printer_id)
    # Firmware "when known": a printer that is offline or has not reported yet is
    # judged by its model now and checked again before its run starts.
    firmware = state.firmware_version if state else None
    refusal = drying_preflight.refusal_code(printer.model, firmware, require_firmware=bool(firmware))
    if refusal == "screen_only":
        raise DryingRefused(drying_preflight.SCREEN_ONLY_DETAIL)
    if refusal == "unsupported":
        raise DryingRefused(drying_preflight.UNSUPPORTED_DETAIL)
    if not 1 <= duration_hours <= 24:
        raise DryingRefused("Duration must be 1-24 hours")
    # An unreported unit is held to the ceiling its id implies (AMS-HT from 128),
    # and the run is checked against the real unit again before it starts.
    unit = find_ams_unit(state.raw_data if state else None, ams_id)
    max_temp = drying_preflight.max_temp_for(unit, ams_id)
    if temp < drying_preflight.AMS_DRY_MIN_TEMP or temp > max_temp:
        raise DryingRefused(f"Temperature must be 45-{max_temp}°C for this AMS unit")
    return printer


async def create_run(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    temp: int,
    duration_hours: int,
    filament: str,
    rotate_tray: bool,
    start_after: datetime | None,
    created_by_id: int | None,
) -> ScheduledDrying:
    await validate_target(db, printer_id, ams_id, temp, duration_hours)
    if start_after is not None:
        start_after = to_naive_utc(start_after)
        if start_after <= _utcnow():
            raise DryingRefused("The start time must be in the future")
    run = ScheduledDrying(
        printer_id=printer_id,
        ams_id=ams_id,
        temp=temp,
        duration_hours=duration_hours,
        filament=filament or "",
        rotate_tray=rotate_tray,
        start_after=start_after,
        created_by_id=created_by_id,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def cancel_run(db: AsyncSession, run_id: int) -> str:
    """Cancel a waiting or running run (``"cancelled"``), or clear a failed / skipped one (``"dismissed"``)."""
    run = await db.get(ScheduledDrying, run_id)
    if run is None:
        raise DryingRefused("Scheduled drying not found", 404)
    if run.status in ("failed", "skipped"):
        await db.delete(run)
        await db.commit()
        return "dismissed"
    if run.status not in RUN_ACTIVE:
        raise DryingRefused("Only a pending, running, failed or skipped drying can be cancelled")
    if run.status == "running":
        # Best effort: the row is cancelled even if the printer is offline.
        printer_manager.send_drying_command(run.printer_id, run.ams_id, 0, 0, mode=0)
    run.status = "cancelled"
    run.completed_at = _utcnow()
    await db.commit()
    return "cancelled"


def _check_rule_times(start_time: str, weekdays: int, latest_start: str | None) -> None:
    try:
        parse_hhmm(start_time)
        if latest_start is not None:
            parse_hhmm(latest_start)
    except ValueError as exc:
        raise DryingRefused("Times must be HH:MM") from exc
    if not weekdays & 0b1111111:
        raise DryingRefused("Choose at least one day of the week")


async def _drop_pending_runs(db: AsyncSession, schedule_id: int) -> None:
    await db.execute(
        delete(ScheduledDrying).where(ScheduledDrying.schedule_id == schedule_id, ScheduledDrying.status == "pending")
    )


async def create_schedule(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    temp: int,
    duration_hours: int,
    filament: str,
    rotate_tray: bool,
    start_time: str,
    weekdays: int,
    latest_start: str | None,
    enabled: bool,
    created_by_id: int | None,
) -> DryingSchedule:
    await validate_target(db, printer_id, ams_id, temp, duration_hours)
    _check_rule_times(start_time, weekdays, latest_start)
    rule = DryingSchedule(
        printer_id=printer_id,
        ams_id=ams_id,
        temp=temp,
        duration_hours=duration_hours,
        filament=filament or "",
        rotate_tray=rotate_tray,
        start_time=start_time,
        weekdays=weekdays,
        latest_start=latest_start,
        enabled=enabled,
        created_by_id=created_by_id,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


_RULE_FIELDS = (
    "ams_id",
    "temp",
    "duration_hours",
    "filament",
    "rotate_tray",
    "start_time",
    "weekdays",
    "latest_start",
    "enabled",
)


async def update_schedule(db: AsyncSession, schedule_id: int, changes: dict) -> DryingSchedule:
    rule = await db.get(DryingSchedule, schedule_id)
    if rule is None:
        raise DryingRefused("Drying schedule not found", 404)
    merged = {f: changes[f] if f in changes else getattr(rule, f) for f in _RULE_FIELDS}
    # Pausing, renaming the filament or moving the time asks nothing of the printer:
    # only a new target is checked against it, so an offline printer's rule stays editable.
    if any(merged[f] != getattr(rule, f) for f in ("ams_id", "temp", "duration_hours")):
        await validate_target(db, rule.printer_id, merged["ams_id"], merged["temp"], merged["duration_hours"])
    _check_rule_times(merged["start_time"], merged["weekdays"], merged["latest_start"])
    timing_changed = merged["start_time"] != rule.start_time or merged["weekdays"] != rule.weekdays
    for name in _RULE_FIELDS:
        setattr(rule, name, merged[name])
    rule.updated_at = _utcnow()
    if timing_changed or not rule.enabled:
        # A different night (or none): the next tick materialises from now.
        await _drop_pending_runs(db, schedule_id)
    else:
        # Same night: the waiting run keeps its occurrence and takes the new
        # parameters — an edit inside tonight's window must not move it to tomorrow.
        await _refresh_pending_runs(db, rule)
    # A running run finishes as it started.
    await db.commit()
    await db.refresh(rule)
    return rule


async def _refresh_pending_runs(db: AsyncSession, rule: DryingSchedule) -> None:
    runs = (
        (
            await db.execute(
                select(ScheduledDrying).where(
                    ScheduledDrying.schedule_id == rule.id, ScheduledDrying.status == "pending"
                )
            )
        )
        .scalars()
        .all()
    )
    tz = server_timezone()
    for run in runs:
        run.ams_id, run.temp, run.duration_hours = rule.ams_id, rule.temp, rule.duration_hours
        run.filament, run.rotate_tray = rule.filament, rule.rotate_tray
        if run.start_after is not None:
            end = window_end(as_aware_utc(run.start_after), rule.start_time, rule.latest_start, rule.weekdays, tz)
            run.latest_start = to_naive_utc(end)


async def delete_schedule(db: AsyncSession, schedule_id: int) -> None:
    rule = await db.get(DryingSchedule, schedule_id)
    if rule is None:
        raise DryingRefused("Drying schedule not found", 404)
    await _drop_pending_runs(db, schedule_id)
    # SQLite ignores ON DELETE SET NULL — a running run finishes as a one-shot.
    await db.execute(update(ScheduledDrying).where(ScheduledDrying.schedule_id == schedule_id).values(schedule_id=None))
    await db.delete(rule)
    await db.commit()


async def forget_printer(db: AsyncSession, printer_id: int, *, archived: bool, commit: bool = True) -> None:
    """Archived: cancel what waits, disable the rules. Deleted: remove both (SQLite runs no FK actions).

    A running cycle is stopped either way. ``commit=False`` for a caller whose own
    transaction this is part of — the printer's delete commits once, at its end.
    """
    running = (
        (
            await db.execute(
                select(ScheduledDrying).where(
                    ScheduledDrying.printer_id == printer_id, ScheduledDrying.status == "running"
                )
            )
        )
        .scalars()
        .all()
    )
    for run in running:
        printer_manager.send_drying_command(printer_id, run.ams_id, 0, 0, mode=0)
    if archived:
        await db.execute(
            update(ScheduledDrying)
            .where(ScheduledDrying.printer_id == printer_id, ScheduledDrying.status.in_(RUN_ACTIVE))
            .values(status="cancelled", completed_at=_utcnow())
        )
        await db.execute(update(DryingSchedule).where(DryingSchedule.printer_id == printer_id).values(enabled=False))
    else:
        await db.execute(delete(ScheduledDrying).where(ScheduledDrying.printer_id == printer_id))
        await db.execute(delete(DryingSchedule).where(DryingSchedule.printer_id == printer_id))
    if commit:
        await db.commit()


# ---------------------------------------------------------------------------
# The tick — called by PrintScheduler once per pass, in its own session.
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    running_printers: set[int] = field(default_factory=set)
    reserved_units: set[tuple[int, int]] = field(default_factory=set)


def _ams_label(ams_id: int) -> str:
    return f"HT-{chr(65 + (ams_id - 128))}" if ams_id >= 128 else f"AMS-{chr(65 + ams_id)}"


async def _describe(db: AsyncSession, run: ScheduledDrying) -> tuple[str, str]:
    """(printer name, schedule description) for a notification."""
    printer = await db.get(Printer, run.printer_id)
    name = printer.name if printer else f"Printer {run.printer_id}"
    schedule = ""
    if run.schedule_id is not None:
        rule = await db.get(DryingSchedule, run.schedule_id)
        if rule is not None:
            schedule = f"↻ {rule.start_time}"
    return name, schedule


async def _notify(db: AsyncSession, kind: str, run: ScheduledDrying) -> None:
    """Never raises: a provider being down must not undo what the tick just decided."""
    try:
        name, schedule = await _describe(db, run)
        label = _ams_label(run.ams_id)
        if kind == "started":
            await notification_service.on_scheduled_drying_started(
                run.printer_id, name, label, run.filament, run.temp, run.duration_hours, schedule, db
            )
        elif kind == "completed":
            await notification_service.on_scheduled_drying_completed(
                run.printer_id, name, label, run.temp, run.duration_hours, schedule, db
            )
        else:
            lang = current_language()
            reason = reason_text(run.reason, lang)
            if run.detail:
                reason = f"{reason} ({reason_text(run.detail, lang)})"
            await notification_service.on_scheduled_drying_failed(run.printer_id, name, label, reason, schedule, db)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("Scheduled drying %s notification failed for run %s: %s", kind, run.id, exc)


async def _prune(db: AsyncSession, now: datetime) -> None:
    global _last_prune
    stamp = monotonic_clock.monotonic()
    if _last_prune is not None and stamp - _last_prune < PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = stamp
    await db.execute(
        delete(ScheduledDrying).where(
            ScheduledDrying.status.in_(("completed", "cancelled", "failed", "skipped")),
            ScheduledDrying.completed_at.is_not(None),
            ScheduledDrying.completed_at < now - timedelta(days=RETENTION_DAYS),
        )
    )


async def _materialise(db: AsyncSession, now: datetime) -> None:
    """Give every enabled rule without a waiting or running run its next one."""
    tz = server_timezone()
    rules = (await db.execute(select(DryingSchedule).where(DryingSchedule.enabled.is_(True)))).scalars().all()
    for rule in rules:
        active = await db.scalar(
            select(ScheduledDrying.id)
            .where(ScheduledDrying.schedule_id == rule.id, ScheduledDrying.status.in_(RUN_ACTIVE))
            .limit(1)
        )
        if active is not None:
            continue
        last = await db.scalar(
            select(ScheduledDrying.start_after)
            .where(ScheduledDrying.schedule_id == rule.id)
            .order_by(ScheduledDrying.start_after.desc())
            .limit(1)
        )
        after = max((x for x in (rule.updated_at, last) if x is not None), default=now)
        try:
            occurrence = next_occurrence(rule.start_time, rule.weekdays, as_aware_utc(after), tz)
            deadline = window_end(occurrence, rule.start_time, rule.latest_start, rule.weekdays, tz)
            # Nights whose window closed while the server was down (or before the
            # rule existed) are passed over silently — materialising them would
            # only produce a string of "did not happen" notifications on restart.
            for _ in range(400):
                if deadline > as_aware_utc(now):
                    break
                occurrence = next_occurrence(rule.start_time, rule.weekdays, occurrence, tz)
                deadline = window_end(occurrence, rule.start_time, rule.latest_start, rule.weekdays, tz)
        except ValueError as exc:
            logger.warning("Drying schedule %s cannot run: %s", rule.id, exc)
            continue
        db.add(
            ScheduledDrying(
                printer_id=rule.printer_id,
                ams_id=rule.ams_id,
                temp=rule.temp,
                duration_hours=rule.duration_hours,
                filament=rule.filament,
                rotate_tray=rule.rotate_tray,
                schedule_id=rule.id,
                start_after=to_naive_utc(occurrence),
                latest_start=to_naive_utc(deadline),
            )
        )
    await db.flush()


def _finish(run: ScheduledDrying, status: str, now: datetime, *, reason: str | None = None, detail: str | None = None):
    run.status = status
    run.completed_at = now
    if reason is not None:
        run.reason = reason
    if detail is not None:
        run.detail = detail


async def _try_start(
    db: AsyncSession,
    run: ScheduledDrying,
    now: datetime,
    dispatching_printers: set[int],
    started: set[tuple[int, int]],
) -> None:
    if run.latest_start is not None and run.latest_start <= now:
        # The last waiting reason becomes the detail: "its start window passed (the printer was printing)".
        _finish(run, "skipped", now, detail=run.reason, reason="window_passed")
        logger.info("Scheduled drying: run %s on printer %s skipped — its window passed", run.id, run.printer_id)
        await _notify(db, "failed", run)
        return
    printer = await db.get(Printer, run.printer_id)
    if printer is None:
        return
    state = printer_manager.get_status(run.printer_id)
    # ⚠️ Reachability first. A client exists from the moment it is created, before
    # the printer says anything — no firmware, no AMS — and after every reconnect:
    # judged as it stands, that empty state read as "firmware cannot dry".
    if state is None or not printer_manager.is_connected(run.printer_id) or not state.raw_data:
        run.reason = "printer_offline"
        return
    firmware = state.firmware_version
    refusal = drying_preflight.refusal_code(printer.model, firmware, require_firmware=bool(firmware))
    if refusal is not None:
        _finish(run, "failed", now, reason=refusal)
        logger.info("Scheduled drying: run %s on printer %s refused (%s)", run.id, run.printer_id, refusal)
        await _notify(db, "failed", run)
        return
    unit = find_ams_unit(state.raw_data, run.ams_id)
    if unit is None:
        run.reason = "ams_not_found"
        return
    if run.temp > drying_preflight.max_temp_for_unit(unit):
        _finish(run, "failed", now, reason="temp_over_limit")
        logger.info("Scheduled drying: run %s on printer %s above the unit's ceiling", run.id, run.printer_id)
        await _notify(db, "failed", run)
        return
    # Busy before the AMS: a printing printer reports its own blocker code (0), and
    # "the printer was printing" is what the operator needs to read.
    if run.printer_id in dispatching_printers or (state.state or "").upper() in _BUSY_STATES:
        run.reason = "printer_busy"
        return
    if is_drying_active(unit) or (run.printer_id, run.ams_id) in started:
        run.reason = "already_drying"
        return
    blocker = drying_preflight.blocker_code(unit)
    if blocker is not None:
        run.reason = blocker
        return
    filament = drying_preflight.resolve_filament(unit, run.filament)
    if not printer_manager.send_drying_command(
        run.printer_id,
        run.ams_id,
        run.temp,
        run.duration_hours,
        mode=1,
        filament=filament,
        rotate_tray=run.rotate_tray,
    ):
        run.reason = "printer_offline"
        return
    run.status, run.started_at, run.reason, run.detail, run.filament = "running", now, None, None, filament
    started.add((run.printer_id, run.ams_id))
    # The heater is on: nothing later in this pass may roll that back into "pending",
    # or the next pass would start the same cycle a second time.
    await db.commit()
    logger.info(
        "Scheduled drying: started run %s on printer %s AMS %s (%s °C, %s h)",
        run.id,
        run.printer_id,
        run.ams_id,
        run.temp,
        run.duration_hours,
    )
    await _notify(db, "started", run)


async def _yield_to_print(db: AsyncSession, run: ScheduledDrying, now: datetime) -> None:
    """A print has the printer: stop the cycle; it resumes after the print, within its window."""
    printer_manager.send_drying_command(run.printer_id, run.ams_id, 0, 0, mode=0)
    elapsed = (now - run.started_at).total_seconds() if run.started_at else 0
    _seen_active.discard(run.id)
    if elapsed >= run.duration_hours * 3600 * COMPLETE_FRACTION:
        _finish(run, "completed", now)  # stopped in its cooling tail — it did its job
        logger.info("Scheduled drying: run %s on printer %s done (stopped for a print)", run.id, run.printer_id)
        await _notify(db, "completed", run)
    else:
        run.status, run.started_at, run.reason = "pending", None, "interrupted"
        logger.info("Scheduled drying: run %s on printer %s yields to a print", run.id, run.printer_id)


async def _follow(db: AsyncSession, run: ScheduledDrying, now: datetime) -> None:
    if run.started_at is None:
        run.started_at = now
        return
    elapsed = (now - run.started_at).total_seconds()
    if elapsed < START_GRACE_SECONDS:
        return
    state = printer_manager.get_status(run.printer_id)
    # Offline, or back but not reported yet: "not reported" is not "not drying" —
    # decide when the unit is seen again, and keep holding the printer meanwhile.
    if state is None or not printer_manager.is_connected(run.printer_id):
        return
    unit = find_ams_unit(state.raw_data, run.ams_id)
    if unit is None:
        return
    printing = (state.state or "").upper() in _BUSY_STATES
    if is_drying_active(unit):
        _seen_active.add(run.id)
        if printing:
            # A print that did not come through the queue — Print now, the printer's
            # screen, the slicer — takes the printer the same way (owner's decision 6).
            await _yield_to_print(db, run, now)
        return
    if elapsed >= run.duration_hours * 3600 * COMPLETE_FRACTION:
        _seen_active.discard(run.id)
        _finish(run, "completed", now)
        logger.info("Scheduled drying: run %s on printer %s completed", run.id, run.printer_id)
        await _notify(db, "completed", run)
    elif printing:
        _seen_active.discard(run.id)
        run.status, run.started_at, run.reason = "pending", None, "interrupted"
        logger.info("Scheduled drying: run %s on printer %s interrupted by a print", run.id, run.printer_id)
    elif run.id not in _seen_active and elapsed < START_CONFIRM_SECONDS:
        # Accepted, never begun: the operator was told it started, so say it did not.
        _finish(run, "failed", now, reason="not_started")
        logger.info("Scheduled drying: run %s on printer %s never started", run.id, run.printer_id)
        await _notify(db, "failed", run)
    else:
        _seen_active.discard(run.id)
        _finish(run, "cancelled", now)  # stopped by hand on an idle printer — not a failure
        logger.info("Scheduled drying: run %s on printer %s stopped early — cancelled", run.id, run.printer_id)


def _any_unit_drying(printer_id: int) -> bool:
    state = printer_manager.get_status(printer_id)
    return bool(state) and any(is_drying_active(u) for u in (state.raw_data or {}).get("ams") or [])


async def tick(
    db: AsyncSession, *, now: datetime, drying_in_progress: dict[int, float], dispatching_printers: set[int]
) -> TickResult:
    """One pass: prune, materialise rules, start what is due, follow what runs, release what ended.

    ``now`` is naive UTC. ``drying_in_progress`` is the scheduler's hold dict,
    shared with auto-drying: a running run adds its printer, and a printer whose
    run ended is released only when nothing else dries there.
    """
    global _last_running
    await _prune(db, now)
    await _materialise(db, now)
    runs = (
        (
            await db.execute(
                select(ScheduledDrying)
                .where(ScheduledDrying.status.in_(RUN_ACTIVE))
                .order_by(ScheduledDrying.start_after.asc().nullsfirst(), ScheduledDrying.id.asc())
            )
        )
        .scalars()
        .all()
    )
    started: set[tuple[int, int]] = set()
    # Printers whose run was running when the pass began: one that ends during this
    # pass must be released too, not only one that ended between passes.
    was_running = {run.printer_id for run in runs if run.status == "running"}
    for run in runs:
        if run.status == "running":
            await _follow(db, run, now)
        elif run.start_after is None or run.start_after <= now:
            await _try_start(db, run, now, dispatching_printers, started)

    result = TickResult()
    for run in runs:
        due = run.start_after is None or run.start_after <= now
        if run.status == "running":
            result.running_printers.add(run.printer_id)
            result.reserved_units.add((run.printer_id, run.ams_id))
        elif run.status == "pending" and due:
            result.reserved_units.add((run.printer_id, run.ams_id))
    for pid in result.running_printers:
        drying_in_progress.setdefault(pid, monotonic_clock.monotonic())
    # Release printers whose run ended — this pass, or through the route between passes —
    # but only if nothing else is drying on them: the dict is shared with auto-drying.
    for pid in (_last_running | was_running) - result.running_printers:
        if not _any_unit_drying(pid):
            drying_in_progress.pop(pid, None)
    _last_running = set(result.running_printers)
    await db.commit()
    return result


async def preempt_for_print(db: AsyncSession, printer_id: int, now: datetime) -> int:
    """A print is going out on this printer: stop its scheduled cycles; they resume when it is free."""
    runs = (
        (
            await db.execute(
                select(ScheduledDrying).where(
                    ScheduledDrying.printer_id == printer_id, ScheduledDrying.status == "running"
                )
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        await _yield_to_print(db, run, now)
    if runs:
        await db.commit()
    return len(runs)


async def running_printer_ids(db: AsyncSession) -> set[int]:
    rows = await db.execute(select(ScheduledDrying.printer_id).where(ScheduledDrying.status == "running"))
    return set(rows.scalars().all())
