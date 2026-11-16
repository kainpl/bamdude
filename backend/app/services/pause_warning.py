"""The "pause coming up" heads-up: one event per slicer pause per run.

``observe`` is called on every printer status and only looks at memory
(``pause_forecast``); the lead-time setting is read in a task, at most once per
``_LEAD_CACHE_SECONDS``, and only while a pause is close. Nothing here writes to
the printer, the queue, the archive or ``PrinterState``.

When it fires (``pause_warning_minutes`` = the lead, 0 = never):

* the forecast has a time (Standard speed, a file with time markers): once the
  estimate is at or below the lead;
* the forecast has only a layer: once the print is on the layer before the pause.

Once per pause per run: the record is ``(archive recovery identity, pause
layer)`` -- one archive per physical print, so a reprint is another run and is
warned about again; never ``subtask_id``, never a printer id alone (SQLite gives
a deleted printer's id to the next one). The record lives in this process: after
a restart it is empty, so a run that is RECOVERED (bound by ``exact_subtask`` to
an archive that was already printing) and is first seen already inside the lead
window is taken as warned -- that one heads-up may be lost, none is ever sent
twice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from backend.app.core.database import async_session
from backend.app.core.tasks import spawn_background_task
from backend.app.services.pause_forecast import PauseForecast, forecast_next_pause
from backend.app.services.print_run_binding import current_print_run

logger = logging.getLogger(__name__)

DEFAULT_WARNING_MINUTES = 5
# The largest lead the setting allows. A pause further away is not a reason to read the setting.
MAX_WARNING_MINUTES = 120
_LEAD_CACHE_SECONDS = 30.0


@dataclass
class _Run:
    identity: str
    warned: set[int] = field(default_factory=set)
    # A run found again after this process lost its memory; cleared the first
    # time the run is seen outside a lead window, or when it spends its one skip.
    recovered: bool = False


_runs: dict[int, _Run] = {}
_in_flight: set[int] = set()
_lead_cache: tuple[float, int] | None = None


def reset() -> None:
    """Forget everything (tests, and a restore that replaces the settings)."""
    global _lead_cache
    _runs.clear()
    _in_flight.clear()
    _lead_cache = None


def _due(forecast: PauseForecast, lead: int) -> bool:
    if lead <= 0:
        return False
    if forecast.minutes is not None:
        return forecast.minutes <= lead
    return forecast.layer_now >= forecast.layer - 1


def _run_of(manager, forecast: PauseForecast) -> _Run:
    run = _runs.get(forecast.printer_id)
    if run is None or run.identity != forecast.archive_identity:
        bound = current_print_run(manager, forecast.printer_id)
        run = _Run(forecast.archive_identity, recovered=getattr(bound, "origin", None) == "exact_subtask")
        _runs[forecast.printer_id] = run
    return run


def observe(manager, printer_id: int):
    """Called on every status. Returns the task it started, or ``None``."""
    forecast = forecast_next_pause(manager, printer_id)
    if forecast is None:
        return None
    run = _run_of(manager, forecast)
    if forecast.layer in run.warned or printer_id in _in_flight:
        return None
    cached = _lead_cache
    lead = cached[1] if cached is not None and time.monotonic() - cached[0] < _LEAD_CACHE_SECONDS else None
    if not _due(forecast, MAX_WARNING_MINUTES if lead is None else lead):
        if lead is None or lead > 0:
            run.recovered = False
        return None
    _in_flight.add(printer_id)
    return spawn_background_task(
        _warn_if_due(manager, printer_id, forecast.archive_identity, forecast.layer),
        name=f"pause-warning-{printer_id}",
    )


async def warning_minutes(db) -> int:
    """The lead-time setting; the default for a missing or broken value, clamped to its range."""
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "pause_warning_minutes")
    try:
        minutes = DEFAULT_WARNING_MINUTES if value in (None, "", "None") else int(value)
    except (TypeError, ValueError):
        return DEFAULT_WARNING_MINUTES
    return min(max(minutes, 0), MAX_WARNING_MINUTES)


async def _load(db, printer_id: int) -> tuple[int, str | None]:
    from backend.app.models.printer import Printer

    lead = await warning_minutes(db)
    printer = await db.get(Printer, printer_id)
    return lead, printer.name if printer is not None else None


async def _warn_if_due(manager, printer_id: int, identity: str, layer: int) -> bool:
    global _lead_cache
    from backend.app.services.notification_service import notification_service

    try:
        async with async_session() as db:
            lead, printer_name = await _load(db, printer_id)
            _lead_cache = (time.monotonic(), lead)
            # Read again after the await: the print may have moved on, ended, or the
            # id may name another printer record by now -- a forecast is given only
            # for the current life of the record and the run its grant names.
            forecast = forecast_next_pause(manager, printer_id)
            run = _runs.get(printer_id)
            if (
                forecast is None
                or run is None
                or run.identity != identity
                or forecast.archive_identity != identity
                or forecast.layer != layer
                or layer in run.warned
            ):
                return False
            if not _due(forecast, lead):
                if lead > 0:
                    run.recovered = False
                return False
            # Marked before the send, with no await in between: a second status cannot send it again.
            run.warned.add(layer)
            if run.recovered:
                run.recovered = False
                logger.info(
                    "[PAUSE WARNING] printer %s: the pause at layer %s was already inside the lead time when this "
                    "run was found again; not sent (it may have been sent before the restart)",
                    printer_id,
                    layer,
                )
                return False
            if printer_name is None:
                return False
            state = manager._clients[printer_id].state
            filename = state.subtask_name or state.gcode_file or "Unknown"
            await notification_service.on_pause_coming_up(
                printer_id=printer_id,
                printer_name=printer_name,
                filename=filename,
                layer=forecast.layer,
                total_layers=forecast.total_layers,
                minutes=forecast.minutes,
                db=db,
            )
            return True
    except Exception as exc:  # noqa: BLE001 -- a heads-up must never break the status path
        logger.warning("Pause-coming-up notification failed for printer %s: %s", printer_id, exc)
        return False
    finally:
        _in_flight.discard(printer_id)
