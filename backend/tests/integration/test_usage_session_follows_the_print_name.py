"""The usage session follows its print through the printer's renaming of it (GH #115).

After a power cycle an A1 mini (firmware 01.08.01.00) reports ``subtask_name`` as the
file name -- ``LID_600_10mm_x10.3mf`` -- instead of the ``LID_600_10mm_x10`` BamDude
sent and the session recorded at start. The firmware also folds spaces into
underscores, while an archive keeps the uploaded name. The three readers that ask
"does this persisted session belong to this print" compared raw strings. A restart
mid-print therefore threw the session (tray chronology, dispatched mapping) away, and a
reconcile-driven completion booked without it.

They now ask the same question the reconcile and live-archive readers ask
(``print_reconciliation._subtask_norm``): the same print, whatever extension or space
form the name came back in. A different print is still refused.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401 -- resolves Printer's relationship
from backend.app.models.active_print_session import ActivePrintSession
from backend.app.models.archive import PrintArchive

pytestmark = pytest.mark.integration

STARTED_AS = "LID_600_10mm_x10"


async def _persisted(db, printer_id: int, print_name: str, log=None) -> None:
    db.add(
        ActivePrintSession(
            printer_id=printer_id,
            print_name=print_name,
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            tray_now_at_start=254,
            tray_change_log=log,
        )
    )
    await db.commit()


def _state(subtask_name: str):
    return SimpleNamespace(
        subtask_name=subtask_name, tray_change_log=[], tray_now=254, layer_num=30, last_loaded_tray=-1
    )


@pytest.mark.parametrize(
    "running",
    ["LID_600_10mm_x10.3mf", "LID_600_10mm_x10.gcode.3mf", "LID 600 10mm x10", "lid_600_10mm_x10"],
)
async def test_a_restart_keeps_the_session_the_printer_renamed(db_session, printer_factory, running):
    from backend.app.main import _restore_usage_tracking_session

    printer = await printer_factory()
    await _persisted(db_session, printer.id, STARTED_AS, log=[[254, 0], [254, 25]])
    state = _state(running)

    assert await _restore_usage_tracking_session(printer.id, state, db_session, logging.getLogger(__name__))

    assert await db_session.get(ActivePrintSession, printer.id, populate_existing=True) is not None
    assert state.tray_change_log == [(254, 0), (254, 25)]


async def test_a_restart_still_discards_another_prints_session(db_session, printer_factory):
    from backend.app.main import _restore_usage_tracking_session

    printer = await printer_factory()
    await _persisted(db_session, printer.id, STARTED_AS, log=[[254, 0], [254, 25]])
    state = _state("Body_1400_New_x4.3mf")

    assert await _restore_usage_tracking_session(printer.id, state, db_session, logging.getLogger(__name__))

    assert await db_session.get(ActivePrintSession, printer.id, populate_existing=True) is None
    assert state.tray_change_log == [(254, 30)]  # seeded from the tray feeding now, not the old chronology


async def _printing_archive(db, printer_id: int, *, print_name: str, filename: str) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        print_name=print_name,
        file_path=f"x/{filename}",
        file_size=1,
        status="printing",
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def test_a_reconcile_close_clears_the_session_of_its_renamed_print(db_session, printer_factory):
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    printer = await printer_factory()
    archive = await _printing_archive(
        db_session, printer.id, print_name="LID 600 10mm x10", filename="LID 600 10mm x10.gcode.3mf"
    )
    await _persisted(db_session, printer.id, STARTED_AS)

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock):
        await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert await db_session.get(ActivePrintSession, printer.id, populate_existing=True) is None


async def test_a_reconcile_close_leaves_another_prints_session(db_session, printer_factory):
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    printer = await printer_factory()
    archive = await _printing_archive(
        db_session, printer.id, print_name="LID 600 10mm x10", filename="LID 600 10mm x10.gcode.3mf"
    )
    await _persisted(db_session, printer.id, "Body_1400_New_x4")

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock):
        await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert await db_session.get(ActivePrintSession, printer.id, populate_existing=True) is not None


@pytest.mark.parametrize(
    ("session_name", "print_name", "filename", "belongs"),
    [
        (STARTED_AS, "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf", True),
        ("LID_600_10mm_x10.3mf", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf", True),
        # A multi-plate archive's print_name carries " - Plate N"; its filename answers.
        ("Bracket", "Bracket - Plate 2", "Bracket.gcode.3mf", True),
        ("Body_1400_New_x4", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf", False),
    ],
)
async def test_the_tracker_lends_a_session_only_to_its_own_print(
    db_session, printer_factory, session_name, print_name, filename, belongs
):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    archive = await _printing_archive(db_session, printer.id, print_name=print_name, filename=filename)
    session = ut.PrintSession(printer_id=printer.id, print_name=session_name, started_at=datetime.now(timezone.utc))
    ut._active_sessions[printer.id] = session
    try:
        with patch.object(ut, "restore_session", new_callable=AsyncMock):
            try:
                await ut.on_print_complete(
                    printer.id,
                    {"status": "completed"},
                    printer_manager=None,
                    db=db_session,
                    archive_id=archive.id,
                    expected_print_name=archive.print_name,
                    archive_snapshot=archive,
                )
            except Exception:  # noqa: BLE001 -- booking on the bare fixture may fail; the guard is the subject
                pass
        put_back = ut._active_sessions.get(printer.id) is session
        assert put_back is not belongs
    finally:
        ut._active_sessions.pop(printer.id, None)
