"""Every physical print is one archive row; there is no "auto-archive" switch.

``printers.auto_archive`` came with the fork. BamDude never offered the switch
in its forms (m002 forced TRUE), but the column was still read: a row holding
False made the print-start handler return before the archive step — and, for a
print BamDude itself dispatched, before the usage session was claimed and the
energy baseline taken. The readers are gone; the column stays (migrations are
frozen) and decides nothing.
"""

from __future__ import annotations

import inspect
import re

import pytest

from backend.app import main
from backend.tests.integration.test_archive_row_at_print_start import (  # noqa: F401
    _clear_active_prints,
    _drive_print_start,
    _Recorder,
)


def test_nothing_in_the_print_callbacks_reads_the_column():
    assert not re.search(r"\.auto_archive\b", inspect.getsource(main))


def test_the_api_neither_accepts_nor_reports_it():
    from backend.app.schemas.printer import PrinterCreate, PrinterResponse, PrinterUpdate

    for schema in (PrinterCreate, PrinterUpdate, PrinterResponse):
        assert "auto_archive" not in schema.model_fields, schema.__name__


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_whose_row_says_no_still_gets_its_print_archived(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    printer = await printer_factory()
    printer.auto_archive = False
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    await db_session.commit()
    printer_id = printer.id
    recorder = _Recorder()

    rows = await _drive_print_start(
        db_session=db_session,
        test_engine=test_engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        printer=printer,
        download_result=None,
        recorder=recorder,
    )

    assert len(rows) == 1
    assert rows[0].status == "printing"
    assert rows[0].printer_id == printer_id
    assert "archive_created" in recorder.seq
    assert "energy_start" in recorder.seq
