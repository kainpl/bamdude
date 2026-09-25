"""Scheduled AMS drying — the ONE writer of ``drying_schedules`` and ``scheduled_dryings``.

Spec: vault 60-specs/scheduled-drying-spec. Routes and ``PrintScheduler`` call
this module; nothing else writes the two tables. A rule's time is wall-clock in
the SERVER's timezone (``core/timezones.server_timezone()``): what the server does
on its own schedule belongs to the farm, not to whichever browser created it.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.services import drying_preflight
from backend.app.services.printer_manager import find_ams_unit, printer_manager

logger = logging.getLogger(__name__)

UTC = timezone.utc

RUN_ACTIVE = ("pending", "running")
RUN_LISTED = ("pending", "running", "failed", "skipped")
_DATA = Path(__file__).resolve().parents[1] / "data"


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
    refusal = drying_preflight.refusal_code(
        printer.model, state.firmware_version if state else None, require_firmware=state is not None
    )
    if refusal == "screen_only":
        raise DryingRefused(drying_preflight.SCREEN_ONLY_DETAIL)
    if refusal == "unsupported":
        raise DryingRefused(drying_preflight.UNSUPPORTED_DETAIL)
    if not 1 <= duration_hours <= 24:
        raise DryingRefused("Duration must be 1-24 hours")
    # An offline printer's unit is unknown: the lower fallback ceiling applies now,
    # and the run is checked against the real unit again before it starts.
    unit = find_ams_unit(state.raw_data if state else None, ams_id)
    max_temp = drying_preflight.max_temp_for_unit(unit)
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
    await validate_target(db, rule.printer_id, merged["ams_id"], merged["temp"], merged["duration_hours"])
    _check_rule_times(merged["start_time"], merged["weekdays"], merged["latest_start"])
    for field in _RULE_FIELDS:
        setattr(rule, field, merged[field])
    rule.updated_at = _utcnow()
    # A waiting run carries the old parameters; the next tick re-creates it from
    # the rule. A running one finishes as it started.
    await _drop_pending_runs(db, schedule_id)
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_schedule(db: AsyncSession, schedule_id: int) -> None:
    rule = await db.get(DryingSchedule, schedule_id)
    if rule is None:
        raise DryingRefused("Drying schedule not found", 404)
    await _drop_pending_runs(db, schedule_id)
    # SQLite ignores ON DELETE SET NULL — a running run finishes as a one-shot.
    await db.execute(update(ScheduledDrying).where(ScheduledDrying.schedule_id == schedule_id).values(schedule_id=None))
    await db.delete(rule)
    await db.commit()


async def forget_printer(db: AsyncSession, printer_id: int, *, archived: bool) -> None:
    """Archived: cancel what waits, disable the rules. Deleted: remove both (SQLite runs no FK actions).

    A running cycle is stopped either way.
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
    await db.commit()
