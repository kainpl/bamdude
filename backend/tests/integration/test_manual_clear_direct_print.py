"""Real Clear API followed by the ordinary direct-dispatch camera boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.order_auto_eject import dispatch_check
from backend.tests.integration.test_plate_answers_defects import _finished_flat, _finished_printer

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"auto_eject": False}])
@pytest.mark.parametrize("require_plate_clear", [False, True])
async def test_clear_api_allows_direct_job_after_auto_run_with_printer_detection_disabled(
    async_client, printer_factory, db_session, monkeypatch, options, require_plate_clear
):
    printer = await printer_factory(plate_detection_enabled=False, require_plate_clear=require_plate_clear)
    archive, _ = await _finished_flat(db_session, printer, 1)
    archive.extra_data = {"dispatch_intent": {"auto_eject": True, "submission_id": "synthetic"}}
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "synthetic-manual-token"
    await db_session.commit()
    pid, aid = printer.id, archive.id
    with _finished_printer():
        response = await async_client.post(
            f"/api/v1/printers/{pid}/clear-plate",
            json={"expected_archive_id": aid, "expected_gate_token": "synthetic-manual-token"},
        )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    current = await db_session.get(Printer, pid)
    finished = await db_session.get(PrintArchive, aid)
    assert not current.awaiting_plate_clear
    assert current.plate_detection_enabled is False
    assert finished.extra_data["plate_clear_source"] == "manual"
    camera = AsyncMock(side_effect=AssertionError("Disabled camera must not block this direct print"))
    monkeypatch.setattr("backend.app.services.plate_detection.check_plate_empty", camera)
    verify = AsyncMock()
    await dispatch_check(db_session, SimpleNamespace(options=options), current, verify, lambda _job: None)
    camera.assert_not_awaited()
    verify.assert_awaited_once()
