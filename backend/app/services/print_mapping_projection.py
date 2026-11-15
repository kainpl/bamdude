"""Which trays the running print was dispatched to — a READ-ONLY view for the printer card.

The card marks the AMS slots a running print pulls from ("P1 / P2"). The
status used to take that from ``raw_data["ams_mapping"]``, a field nothing in
our tree writes, so the marks showed only when a firmware volunteered it.

The source here is the mapping the print's own session holds
(``usage_tracker.PrintSession.ams_mapping`` and its durable row
``active_print_sessions``): the dispatched slicer-slot → global-tray list, the
one usage attribution reads first. So the card shows the trays the charge goes
to — never the live MQTT ``mapping``, which AMS Filament Backup rewrites to the
substitute tray.

⚠️ This module only READS. It never writes a session, a row or printer state,
and nothing in accounting reads it back.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE_STATES = ("RUNNING", "PAUSE")


def _clean(mapping: object) -> list[int]:
    if not isinstance(mapping, list):
        return []
    return [value for value in mapping if isinstance(value, int) and not isinstance(value, bool)]


def _bound_identity(printer_id: int) -> tuple[bool, str | None]:
    """(a run is bound to the printer, that run's archive identity)."""
    from backend.app.services.print_run_binding import current_print_run
    from backend.app.services.printer_manager import printer_manager

    run = current_print_run(printer_manager, printer_id)
    if run is None:
        return False, None
    return True, run.archive_identity


def from_session(printer_id: int | None) -> list[int] | None:
    """The mapping of the session held in memory; ``None`` when there is no such session.

    ``None`` is "this process cannot say" (Spoolman mode keeps no session in
    memory, and a restart loses it until the print is met again) — the caller
    must not turn it into "no trays". A session stamped with another archive
    than the run bound to the printer is a leftover and shows nothing.
    """
    if printer_id is None:
        return None
    from backend.app.services import usage_tracker

    session = usage_tracker._active_sessions.get(printer_id)
    if session is None:
        return None
    _bound, identity = _bound_identity(printer_id)
    if identity is not None and session.archive_identity is not None and identity != session.archive_identity:
        return []
    return _clean(session.ams_mapping)


async def from_rows(db: AsyncSession, printer_ids: Iterable[int]) -> dict[int, list[int]]:
    """The durable rows' mappings for printers with no session in memory, in one read.

    A row is a start's context and may outlive its print (a completion nobody
    saw), so it answers only for the run bound to the printer now: no bound
    run, or a row stamped with another archive, shows nothing.
    """
    from backend.app.models.active_print_session import ActivePrintSession

    wanted = sorted(set(printer_ids))
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            ActivePrintSession.printer_id, ActivePrintSession.ams_mapping, ActivePrintSession.archive_identity
        ).where(ActivePrintSession.printer_id.in_(wanted))
    )
    answer: dict[int, list[int]] = {}
    for printer_id, mapping, row_identity in rows.all():
        bound, identity = _bound_identity(printer_id)
        if not bound:
            continue
        if row_identity is not None and identity is not None and row_identity != identity:
            continue
        answer[printer_id] = _clean(mapping)
    return answer
