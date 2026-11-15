"""A spool that no longer exists when its print is booked: its grams are said, not dropped.

A spool named only by a print's start snapshot or its journal can be deleted between the archive
leaving ``printing`` and the booking's commit. The booking skipped it in silence — the print looked
fully recorded and 10 g were charged to nobody. They are now reported as not recorded, like every
other filament the booking could not place.
"""

from __future__ import annotations

from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED, EVENT_START, KIND_MANUAL
from backend.app.services.usage_tracker import _active_sessions
from backend.app.utils.tray_split import terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import (  # noqa: F401 — fixtures
    _clean_memory,
    history,
    journal,
    next_print_state,
    run_internal,
    session,
    stage,
)

COMPLETED = {
    "status": "completed",
    "last_layer_num": 200,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100.0),
}
AMS = [{"id": 0, "tray": [{"id": n, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11} for n in range(2)]}]
GONE = 987_654


async def test_a_deleted_spool_on_a_mapped_tray_is_reported(db_session, stage):  # noqa: F811
    _active_sessions[stage.printer.id] = session(
        stage.printer.id, spool_assignments={(0, 0): GONE, (0, 1): stage.b}, started_at=None, ams_mapping=[0, 1]
    )
    usage = [{"slot_id": 1, "used_g": 10.0, "type": "PLA"}, {"slot_id": 2, "used_g": 20.0, "type": "PLA"}]

    _notice, not_recorded = await run_internal(
        db_session, stage.printer, stage.archive, COMPLETED, next_print_state([], ams=AMS), usage=usage
    )

    assert await history(db_session) == {(stage.b, 20.0)}  # the other filament is charged as before
    assert not_recorded == [(0, 10.0)]


async def test_a_deleted_spool_in_a_runout_split_is_reported(db_session, stage):  # noqa: F811
    """Reel A to layer 100 of 200, then a reel that has been deleted since: A's half is charged, the
    other 5 g are named."""
    await journal(
        db_session,
        stage.printer,
        stage.archive,
        [
            (EVENT_START, None, 0, 0, stage.a),
            (EVENT_RUNOUT, KIND_MANUAL, 0, 100, stage.a),
            (EVENT_SPOOL_LOADED, None, 0, 100, None),
        ],
    )
    from sqlalchemy import update

    from backend.app.models.print_usage_event import PrintUsageEvent

    await db_session.execute(
        update(PrintUsageEvent).where(PrintUsageEvent.event == EVENT_SPOOL_LOADED).values(spool_id=GONE)
    )
    await db_session.commit()
    _active_sessions[stage.printer.id] = session(
        stage.printer.id, spool_assignments={(0, 0): stage.a}, started_at=None, ams_mapping=[0]
    )

    _notice, not_recorded = await run_internal(
        db_session,
        stage.printer,
        stage.archive,
        COMPLETED,
        next_print_state([], ams=AMS),
        usage=[{"slot_id": 1, "used_g": 10.0, "type": "PLA"}],
    )

    assert await history(db_session) == {(stage.a, 5.0)}
    assert not_recorded == [(0, 5.0)]
