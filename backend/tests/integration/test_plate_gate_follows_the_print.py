"""The plate gate and its held row follow the physical print, not the button.

Reported from a farm that prints from the printer's own screen and never
presses Clear (2026-09-04): the gate armed after the first print was still
armed at the next print's end, so Clear and Repeat appeared the moment the
printer said FINISH — while ``on_print_complete`` was still fetching the 3MF
and tidying the SD card, minutes on a P1S — and Repeat, finding no completed
row yet, answered ``409 No finished print is waiting``.

Three things close that: a print STARTING answers the previous question (the
part is off the bed, or the operator decided it is), a print already running
when BamDude joins claims its row like any other print, and a row never points
at an archive that a later branch threw away.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

pytestmark = pytest.mark.integration

START = {"filename": "/data/Metadata/plate_1.gcode", "subtask_name": "cube", "subtask_id": "7"}


@pytest.fixture(autouse=True)
def _clear_active_prints():
    from backend.app.main import _active_prints

    _active_prints.clear()
    yield
    _active_prints.clear()


@pytest.fixture
def main_db(monkeypatch, db_session, test_engine, tmp_path):
    """``on_print_start`` opens its own sessions off the module-level factory."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    return factory


async def _printer_with_queue(db_session, printer_factory):
    printer = await printer_factory(require_plate_clear=True)
    printer.auto_archive = True
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()
    return printer.id


def _quiet_start_patches(pm):
    ws = MagicMock()
    for name in ("send_print_start", "send_archive_created", "send_archive_updated", "broadcast"):
        setattr(ws, name, AsyncMock())
    return (
        patch("backend.app.main.ws_manager", ws),
        patch("backend.app.main.printer_manager", pm),
        patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock, return_value=([], None)),
        patch("backend.app.services.archive_download.try_download_3mf", new_callable=AsyncMock, return_value=None),
        patch(
            "backend.app.services.bambu_ftp.get_ftp_retry_settings",
            new_callable=AsyncMock,
            return_value=(False, 0, 0, 30),
        ),
    )


class TestAPrintStartingAnswersThePreviousQuestion:
    async def test_the_stale_gate_is_released_and_the_held_row_is_let_go(self, db_session, printer_factory, main_db):
        from contextlib import ExitStack

        from backend.app.main import on_print_start

        printer_id = await _printer_with_queue(db_session, printer_factory)
        # The previous print's row, still waiting for an answer nobody gave.
        db_session.add(PrintQueueItem(queue_id=printer_id, status="completed", position=0, archive_id=1))
        await db_session.commit()

        pm = MagicMock()
        pm.get_printer.return_value = None
        pm.get_status.return_value = None
        pm.is_awaiting_plate_clear.return_value = True
        with ExitStack() as stack:
            for p in _quiet_start_patches(pm):
                stack.enter_context(p)
            await on_print_start(printer_id, dict(START))

        pm.set_awaiting_plate_clear.assert_any_call(printer_id, False)
        db_session.expire_all()
        rows = (await db_session.execute(select(PrintQueueItem).order_by(PrintQueueItem.id))).scalars().all()
        # The old completed row is gone; only the new print's claim remains.
        assert [r.status for r in rows] == ["printing"]

    async def test_a_released_gate_is_left_alone(self, db_session, printer_factory, main_db):
        from contextlib import ExitStack

        from backend.app.main import on_print_start

        printer_id = await _printer_with_queue(db_session, printer_factory)
        pm = MagicMock()
        pm.get_printer.return_value = None
        pm.get_status.return_value = None
        pm.is_awaiting_plate_clear.return_value = False
        with ExitStack() as stack:
            for p in _quiet_start_patches(pm):
                stack.enter_context(p)
            await on_print_start(printer_id, dict(START))

        assert (printer_id, False) not in [c.args for c in pm.set_awaiting_plate_clear.call_args_list]


class TestAPrintAlreadyRunningClaimsItsRow:
    async def test_running_observed_claims_the_live_archive(self, db_session, printer_factory, main_db):
        from backend.app.main import on_print_running_observed

        printer_id = await _printer_with_queue(db_session, printer_factory)
        archive = PrintArchive(
            printer_id=printer_id,
            filename="cube.3mf",
            file_path="",
            file_size=0,
            print_name="cube",
            status="printing",
            plate_index=2,
        )
        db_session.add(archive)
        await db_session.commit()
        archive_id = archive.id

        pm = MagicMock()
        pm.get_status.return_value = None
        pm.is_awaiting_plate_clear.return_value = True
        with (
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main._restore_usage_tracking_session", new_callable=AsyncMock),
            patch("backend.app.main._capture_timelapse_baseline_at_start", new_callable=AsyncMock),
        ):
            await on_print_running_observed(
                printer_id, {"subtask_name": "cube", "filename": "/data/Metadata/plate_2.gcode"}
            )

        db_session.expire_all()
        rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
        assert [(r.status, r.archive_id, r.plate_id) for r in rows] == [("printing", archive_id, 2)]
        # A print is running on this bed: the gate from before the restart is moot.
        pm.set_awaiting_plate_clear.assert_any_call(printer_id, False)

    async def test_nothing_is_claimed_without_a_live_archive(self, db_session, printer_factory, main_db):
        from backend.app.main import on_print_running_observed

        printer_id = await _printer_with_queue(db_session, printer_factory)
        pm = MagicMock()
        pm.get_status.return_value = None
        pm.is_awaiting_plate_clear.return_value = False
        with (
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main._restore_usage_tracking_session", new_callable=AsyncMock),
            patch("backend.app.main._capture_timelapse_baseline_at_start", new_callable=AsyncMock),
        ):
            await on_print_running_observed(printer_id, {"subtask_name": "cube"})

        assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []


class TestADiscardedArchiveTakesNoRowWithIt:
    async def test_the_row_is_repointed_at_the_adopted_archive(self, db_session, printer_factory, main_db):
        from backend.app.main import _discard_provisional_archive

        printer_id = await _printer_with_queue(db_session, printer_factory)
        stale = PrintArchive(printer_id=printer_id, filename="a.3mf", file_path="", file_size=0, status="printing")
        adopted = PrintArchive(
            printer_id=printer_id, filename="a.3mf", file_path="x/a.3mf", file_size=1, status="printing"
        )
        db_session.add_all([stale, adopted])
        await db_session.commit()
        row = PrintQueueItem(queue_id=printer_id, status="printing", position=0, archive_id=stale.id)
        db_session.add(row)
        await db_session.commit()
        stale_id, adopted_id, row_id = stale.id, adopted.id, row.id

        with patch("backend.app.main.ws_manager", MagicMock(send_archive_updated=AsyncMock())):
            await _discard_provisional_archive(
                db_session, stale, logging.getLogger("test"), adopted_archive_id=adopted_id
            )

        db_session.expire_all()
        assert await db_session.get(PrintArchive, stale_id) is None
        assert (await db_session.get(PrintQueueItem, row_id)).archive_id == adopted_id


class TestTheCompletionFindsTheRowByThePrintersQueue:
    async def test_a_queue_whose_id_is_not_the_printers_still_matches(self, db_session, printer_factory, main_db):
        """``queue_id == printer_id`` held only by construction; a queue created
        after an orphan squatted on the id breaks it, and the completion then
        found no row to close — for ever."""
        from backend.app.main import _printing_rows_for_printer

        printer = await printer_factory()
        queue = PrinterQueue(id=printer.id + 100, printer_id=printer.id)
        db_session.add(queue)
        await db_session.commit()
        older = PrintQueueItem(queue_id=queue.id, status="printing", position=0, archive_id=1)
        db_session.add(older)
        await db_session.commit()
        newer = PrintQueueItem(queue_id=queue.id, status="printing", position=0, archive_id=2)
        db_session.add(newer)
        await db_session.commit()

        rows = await _printing_rows_for_printer(db_session, printer.id)
        # Newest first: a stale row from an earlier session must not shadow the live one.
        assert [r.id for r in rows] == [newer.id, older.id]
