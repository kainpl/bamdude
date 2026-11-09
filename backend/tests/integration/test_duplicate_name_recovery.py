"""Normalized-name recovery: permanent independent review r1 regressions.

``_live_archive_for_running_print`` resolves the sole printing archive with the
reconciliation name matcher (space/underscore folding, case, truncation marker,
multi-plate suffix through the filename stem). ``on_print_running_observed`` then
asks ``decide_start(..., catch_up=True)``, whose own catch-up matcher is a strict
``print_name ==`` / ``filename in (...)`` comparison. When the two disagree the
live archive found by the first is reported unproven by the second, and the
recovery claims nothing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CASES = [
    # firmware echoes the sanitised stem (spaces -> underscores), print_section of print_reconciliation._subtask_norm
    ("Rear Dry Pod", "Rear Dry Pod.gcode.3mf", "Rear_Dry_Pod"),
    # multi-plate: print_name carries " - Plate N", the filename stem carries the identity
    ("Bracket - Plate 5", "Bracket.gcode.3mf", "Bracket"),
    # truncated echo (upstream #2829)
    (
        "A rather long part name for the farm number 12",
        "A rather long part name for the farm number 12.3mf",
        "A rather long part name for t...",
    ),
]


@pytest.fixture
def main_db(monkeypatch, test_engine, tmp_path):
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    return factory


@pytest.mark.parametrize(("print_name", "filename", "live_subtask"), CASES)
async def test_sole_name_recovery_claims_the_archive_the_resolver_found(
    db_session, printer_factory, main_db, print_name, filename, live_subtask
):
    from backend.app.main import _live_archive_for_running_print, on_print_running_observed
    from backend.app.services.print_reconciliation import _name_matches_subtask

    printer = await printer_factory()
    printer_id = printer.id
    archive = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        print_name=print_name,
        file_path="archive/x.3mf",
        file_size=1,
        status="printing",
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id
    assert _name_matches_subtask(archive, live_subtask)

    pm = MagicMock()
    pm._print_run_bindings = {}
    pm.get_status.return_value = None
    pm.get_client.return_value = None
    pm.is_awaiting_plate_clear.return_value = False
    data = {
        "filename": "/data/Metadata/plate_1.gcode",
        "subtask_name": live_subtask,
        "subtask_id": "0",
        "remaining_time": 600,
        "raw_data": {},
        "ams_mapping": None,
    }
    claim = AsyncMock(return_value=None)
    with (
        patch("backend.app.main.printer_manager", pm),
        patch("backend.app.main.mark_queue_printing_for_printer", claim),
        patch("backend.app.main._capture_timelapse_baseline_at_start", AsyncMock()),
        patch("backend.app.main.spawn_background_task", MagicMock()),
    ):
        live = await _live_archive_for_running_print(printer_id, data)
        assert (live.archive_id, live.reason) == (archive_id, "unambiguous_name")
        await on_print_running_observed(printer_id, dict(data))

    claim.assert_awaited_once()
    assert claim.await_args.kwargs["archive_id"] == archive_id
