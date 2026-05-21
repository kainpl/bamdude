"""Regression test for upstream Bambuddy #1353: layer timelapse must start
for queue / VP-dispatched prints.

Ported from upstream commit f2e3de0a, adapted for BamDude's on_print_start
side-effect surface (we run a few more handlers — macro_trigger, plate
detection, smart-plug, mqtt_relay — but the subject under test is the same
``start_session`` call inside the expected-archive branch).

Root cause (mirrors upstream): only the two new-archive paths in
``on_print_start`` (``fallback_archive`` + the regular auto-archive) called
``layer_timelapse.start_session``. The expected-archive branch — where
every reprint and every queue / VP-dispatched print lands — updated the
existing archive's status to "printing" but never started a timelapse
session. So ``_background_layer_timelapse`` ran at print-complete time,
called ``tl_complete()``, found nothing in ``_active_sessions``, silently
returned ``None``, and every queue / reprint silently lost its timelapse.

Fix: ``start_session`` is now called in the expected-archive branch too,
guarded by the same ``external_camera_enabled and external_camera_url``
check that the other two paths use.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.main import (
    _active_prints,
    _expected_print_creators,
    _expected_print_registered_at,
    _expected_prints,
    _print_ams_mappings,
    register_expected_print,
)


@pytest.fixture(autouse=True)
def _clear_dicts():
    """Clear module-level tracking dicts before and after each test."""
    _expected_prints.clear()
    _expected_print_registered_at.clear()
    _expected_print_creators.clear()
    _print_ams_mappings.clear()
    _active_prints.clear()
    yield
    _expected_prints.clear()
    _expected_print_registered_at.clear()
    _expected_print_creators.clear()
    _print_ams_mappings.clear()
    _active_prints.clear()


def _build_mocks(*, external_camera_enabled: bool, external_camera_url: str | None):
    """Construct a Printer + PrintArchive pair as the on_print_start path expects."""
    mock_printer = MagicMock()
    mock_printer.id = 1
    mock_printer.name = "Test Printer"
    mock_printer.serial_number = "TEST123"
    mock_printer.plate_detection_enabled = False
    mock_printer.external_camera_enabled = external_camera_enabled
    mock_printer.external_camera_url = external_camera_url
    mock_printer.external_camera_type = "snapshot"
    mock_printer.external_camera_snapshot_url = external_camera_url

    mock_archive = MagicMock()
    mock_archive.id = 42
    mock_archive.filename = "Universal_Spirit_level_Holder.3mf"
    mock_archive.created_by_id = None
    mock_archive.printer_id = 1
    mock_archive.print_name = "Universal Spirit Level Holder"
    mock_archive.status = "pending"
    mock_archive.file_path = "/tmp/fake.3mf"
    mock_archive.print_time_seconds = 0

    return mock_printer, mock_archive


def _execute_router(mock_printer, mock_archive):
    """SQL-shape-routed execute mock so the many queries in on_print_start each
    get a sensible response without queueing N mocks in fragile order."""

    def execute(stmt, *args, **kwargs):
        sql = str(stmt).lower()
        if "from printers" in sql or "from printer " in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_printer),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_printer]))),
            )
        if "from print_archives" in sql or "from print_archive" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_archive),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_archive]))),
            )
        # Everything else (settings, queue items, spool assignments) returns empty.
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

    return execute


def _build_session(mock_printer, mock_archive):
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute_router(mock_printer, mock_archive))
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_expected_archive_path_starts_timelapse_when_external_camera_enabled():
    """Queue / VP-dispatched prints land in the expected-archive branch and
    must start the timelapse session there (the #1353 root cause)."""
    mock_printer, mock_archive = _build_mocks(
        external_camera_enabled=True,
        external_camera_url="http://camera.local:5000/snapshot.jpg",
    )

    # Register the expected print so the dispatch flow finds an archive_id.
    register_expected_print(1, "Universal_Spirit_level_Holder.3mf", archive_id=42, ams_mapping=[1])

    mock_session = _build_session(mock_printer, mock_archive)

    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.main.mark_queue_printing_for_printer", new_callable=AsyncMock),
        patch("backend.app.main.maybe_register_external_stagger", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.api.routes.printers.clear_cover_cache"),
        patch("backend.app.services.layer_timelapse.start_session") as mock_start_session,
    ):
        mock_session_maker.return_value = mock_session
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

        from backend.app.main import on_print_start

        await on_print_start(
            1,
            {
                "filename": "Universal_Spirit_level_Holder.3mf",
                "subtask_name": "Universal_Spirit_level_Holder",
            },
        )

        mock_start_session.assert_called_once()
        call_args = mock_start_session.call_args
        # printer_id, expected archive_id, camera URL, camera type
        assert call_args.args[0] == 1, "printer_id must match"
        assert call_args.args[1] == 42, "archive_id must come from the expected-print registration, not a fresh one"
        assert call_args.args[2] == "http://camera.local:5000/snapshot.jpg"
        assert call_args.args[3] == "snapshot"


@pytest.mark.asyncio
async def test_expected_archive_path_skips_timelapse_when_external_camera_disabled():
    """The guard the new-archive paths use must hold here too: no external
    camera → no timelapse session. Otherwise we'd try to capture from a
    None URL."""
    mock_printer, mock_archive = _build_mocks(
        external_camera_enabled=False,
        external_camera_url=None,
    )
    mock_archive.filename = "test.3mf"
    mock_archive.id = 99
    register_expected_print(1, "test.3mf", archive_id=99, ams_mapping=None)

    mock_session = _build_session(mock_printer, mock_archive)

    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.main.mark_queue_printing_for_printer", new_callable=AsyncMock),
        patch("backend.app.main.maybe_register_external_stagger", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.api.routes.printers.clear_cover_cache"),
        patch("backend.app.services.layer_timelapse.start_session") as mock_start_session,
    ):
        mock_session_maker.return_value = mock_session
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

        from backend.app.main import on_print_start

        await on_print_start(1, {"filename": "test.3mf", "subtask_name": "test"})

        mock_start_session.assert_not_called()


async def _run_expected_archive_print_start(mock_printer, mock_archive, filename):
    """Drive on_print_start through the expected-archive branch under the full
    patch stack. Used by the printer_id-assignment regressions below."""
    mock_session = _build_session(mock_printer, mock_archive)
    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.main.mark_queue_printing_for_printer", new_callable=AsyncMock),
        patch("backend.app.main.maybe_register_external_stagger", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.api.routes.printers.clear_cover_cache"),
        patch("backend.app.services.layer_timelapse.start_session"),
    ):
        mock_session_maker.return_value = mock_session
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

        from backend.app.main import on_print_start

        await on_print_start(1, {"filename": filename, "subtask_name": filename.replace(".3mf", "")})


@pytest.mark.asyncio
async def test_expected_archive_path_assigns_printer_id_when_unset():
    """#1403 follow-up: a VP-queue / adopted expected archive created with
    printer_id=None is promoted to the running printer in the expected-archive
    branch, so the timelapse-scan + per-printer paths aren't disabled forever."""
    mock_printer, mock_archive = _build_mocks(external_camera_enabled=False, external_camera_url=None)
    mock_archive.filename = "vp_dispatched.3mf"
    mock_archive.id = 77
    mock_archive.printer_id = None
    register_expected_print(1, "vp_dispatched.3mf", archive_id=77, ams_mapping=None)

    await _run_expected_archive_print_start(mock_printer, mock_archive, "vp_dispatched.3mf")

    assert mock_archive.printer_id == 1


@pytest.mark.asyncio
async def test_expected_archive_path_preserves_existing_printer_id():
    """The assignment is idempotent: an archive that already carries the
    running printer's id stays put (no clobber)."""
    mock_printer, mock_archive = _build_mocks(external_camera_enabled=False, external_camera_url=None)
    mock_archive.filename = "lib_file.3mf"
    mock_archive.id = 88
    mock_archive.printer_id = 1
    register_expected_print(1, "lib_file.3mf", archive_id=88, ams_mapping=None)

    await _run_expected_archive_print_start(mock_printer, mock_archive, "lib_file.3mf")

    assert mock_archive.printer_id == 1
