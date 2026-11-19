"""A print the reconnect sweep closes as FAILED keeps the printer's own fault (upstream 1e30e3266).

A print that failed while BamDude was down or disconnected is closed by the
sweep. The printer still shows the job FAILED with the faults that ended it,
but every such row read "no status update". The reason is now derived from the
printer's present fault stack exactly as the live handler derives it; nothing
else about the close changes (no usage is booked for a failed sweep close).
"""

from types import SimpleNamespace

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services import print_reconciliation
from backend.app.services.print_reconciliation import _live_failure_reason, _reconcile_complete_archive
from backend.app.utils.failure_reasons import NO_STATUS_UPDATE
from backend.tests.integration.swap_terminal_support import an_execution, archive_of, make_rig  # noqa: F401
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _fault(short_code: str) -> SimpleNamespace:
    module_hex, code_hex = short_code.split("_", 1)
    return SimpleNamespace(attr=int(module_hex, 16) << 16, code=f"0x{code_hex}")


def _printer_shows(monkeypatch, faults):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "get_status", lambda _printer_id: SimpleNamespace(hms_errors=faults))


async def _sweep_closes(rig, archive_id, *, status="failed", uncertain=False):
    async with rig.db() as db:
        archive = await db.get(PrintArchive, archive_id)
        await _reconcile_complete_archive(db, archive, status=status, uncertain=uncertain)
        await db.commit()
    return await archive_of(rig, archive_id)


@pytest.mark.parametrize(
    ("faults", "reason"),
    [
        (["0300_8004"], "filamentRunout"),
        (["0F00_FFFF", "0300_4057"], "layerShift"),
        (["0C00_8042"], "spaghettiDetached"),
        (["0F00_FFFF"], NO_STATUS_UPDATE),  # a fault that names no failure
        ([], NO_STATUS_UPDATE),
    ],
)
async def test_a_failed_close_reads_the_printers_fault_stack(make_rig, monkeypatch, faults, reason):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await an_execution(rig, fact=False, bound=False)
    _printer_shows(monkeypatch, [_fault(code) for code in faults])

    archive = await _sweep_closes(rig, archive_id)

    assert archive.status == "failed"
    assert archive.failure_reason == reason


async def test_an_uncertain_close_never_borrows_a_fault(make_rig, monkeypatch):
    """The printer moved on: whatever its stack shows now is not this print's."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await an_execution(rig, fact=False, bound=False)
    _printer_shows(monkeypatch, [_fault("0300_8004")])

    archive = await _sweep_closes(rig, archive_id, status="cancelled", uncertain=True)

    assert (archive.status, archive.failure_reason) == ("cancelled", NO_STATUS_UPDATE)


async def test_a_reason_already_on_the_row_stands(make_rig, monkeypatch):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await an_execution(rig, fact=False, bound=False)
    async with rig.db() as db:
        (await db.get(PrintArchive, archive_id)).failure_reason = "cloggedNozzle"
        await db.commit()
    _printer_shows(monkeypatch, [_fault("0300_8004")])

    archive = await _sweep_closes(rig, archive_id)

    assert archive.failure_reason == "cloggedNozzle"


async def test_an_unreadable_stack_is_no_reason(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "get_status", lambda _printer_id: None)
    assert _live_failure_reason(1) is None
    monkeypatch.setattr(printer_manager, "get_status", lambda _printer_id: SimpleNamespace(hms_errors=[object()]))
    assert _live_failure_reason(1) is None
    assert print_reconciliation._live_failure_reason(None) is None
