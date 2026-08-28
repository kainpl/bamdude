"""The missing-spool-assignment toast reads the dispatched mapping — and on
an AMS-less machine BambuStudio remaps the external holder to ``0`` with
``use_ams=False``. Reading that 0 as AMS0-T0 flagged slot "A1" missing on
every mini external print while the external WAS assigned (2026-08-24)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_assignment_notifications as notif


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr(notif, "async_session", _session_ctx)


def _state(ams_units, vt=None):
    return SimpleNamespace(raw_data={"ams": {"ams": ams_units}, "vt_tray": vt or []})


async def _assign_external(db_session, printer):
    db_session.add(
        SpoolAssignment(
            spool_id=99,
            printer_id=printer.id,
            ams_id=255,
            tray_id=0,
            fingerprint_color="FFFF00FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_amsless_zero_mapping_with_assigned_external_stays_silent(
    db_session, printer_factory, main_db, monkeypatch
):
    printer = await printer_factory(serial_number="NOTIF1")
    await _assign_external(db_session, printer)
    monkeypatch.setattr(notif.printer_manager, "get_status", lambda _pid: _state([], vt=[{"id": 254}]))
    send = AsyncMock()

    with patch.object(notif.ws_manager, "send_missing_spool_assignment", new=send):
        await notif.notify_missing_spool_assignments_on_print_start(
            printer.id, {"ams_mapping": [0]}, __import__("logging").getLogger("t")
        )

    send.assert_not_called()


@pytest.mark.asyncio
async def test_ams_machine_zero_mapping_still_flags_the_real_slot(db_session, printer_factory, main_db, monkeypatch):
    printer = await printer_factory(serial_number="NOTIF2")
    # nothing assigned at AMS0-T0, and the machine really has an AMS
    monkeypatch.setattr(
        notif.printer_manager,
        "get_status",
        lambda _pid: _state([{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "FF0000FF"}]}]),
    )
    send = AsyncMock()

    with (
        patch.object(notif.ws_manager, "send_missing_spool_assignment", new=send),
        patch.object(notif.notification_service, "on_print_missing_spool_assignment", new=AsyncMock()),
    ):
        await notif.notify_missing_spool_assignments_on_print_start(
            printer.id, {"ams_mapping": [0]}, __import__("logging").getLogger("t")
        )

    send.assert_called_once()
    assert send.call_args.kwargs["missing_slots"][0]["slot"] == "A1"


@pytest.mark.asyncio
async def test_amsless_zero_mapping_with_no_assignment_flags_the_external(
    db_session, printer_factory, main_db, monkeypatch
):
    printer = await printer_factory(serial_number="NOTIF3")
    monkeypatch.setattr(notif.printer_manager, "get_status", lambda _pid: _state([], vt=[{"id": 254}]))
    send = AsyncMock()

    with (
        patch.object(notif.ws_manager, "send_missing_spool_assignment", new=send),
        patch.object(notif.notification_service, "on_print_missing_spool_assignment", new=AsyncMock()),
    ):
        await notif.notify_missing_spool_assignments_on_print_start(
            printer.id, {"ams_mapping": [0]}, __import__("logging").getLogger("t")
        )

    send.assert_called_once()
    assert send.call_args.kwargs["missing_slots"][0]["slot"] == "Ext-L"
