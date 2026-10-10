"""Independent reviewer reproductions for PR 67; synthetic inventory only."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.print_usage_event import (
    EVENT_RESUME,
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    KIND_AUTOSWITCH,
    KIND_PAUSE,
    PrintUsageEvent,
)
from backend.app.models.settings import Settings
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.print_usage_journal import record_event
from backend.app.services.usage_tracker import apply_runout_zero_corrections
from backend.tests.unit.services.test_auto_stock_spool import event, manager, printer, spool
from backend.tests.unit.services.test_usage_tracker_runout import _journal, _make_archive


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("same_spool", [True, False])
@pytest.mark.parametrize("preassigned_while_empty", [True, False])
@pytest.mark.parametrize("later_runout_kind", [None, KIND_PAUSE, KIND_AUTOSWITCH])
async def test_manual_correction_after_runout_before_ams_insertion_wins(
    async_client, db_session, printer_factory, tmp_path, same_spool, preassigned_while_empty, later_runout_kind
):
    p = await printer(printer_factory)
    old = await spool(db_session, weight_used=700)
    replacement = await spool(db_session)
    db_session.add(
        SpoolAssignment(
            printer_id=p.id,
            ams_id=0,
            tray_id=0,
            spool_id=old.id,
            fingerprint_type="PLA",
            fingerprint_color=old.rgba,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10),
        )
    )
    await db_session.commit()
    archive = await _make_archive(db_session, p, tmp_path)
    await _journal(db_session, p, archive, [(EVENT_RUNOUT, KIND_PAUSE, 0, 140, old.id)])
    pm, state, tray = manager()
    state.state = "PAUSE"
    tray["state"] = 26
    if preassigned_while_empty:
        tray.update(exists=False, state=9, tray_type="", tray_color="")
    chosen = old if same_spool else replacement
    with (
        patch("backend.app.services.printer_manager.printer_manager.get_status", pm.get_status),
        patch("backend.app.services.printer_manager.printer_manager.ensure_fresh_connection", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=False)),
        patch("backend.app.api.routes.inventory.ws_manager.broadcast", AsyncMock()),
    ):
        response = await async_client.post(
            "/api/v1/inventory/assignments",
            json={
                "printer_id": p.id,
                "ams_id": 0,
                "tray_id": 0,
                "spool_id": chosen.id,
            },
        )
    assert response.status_code == 200, response.text
    # Only after the explicit correction does the operator rethread/reinsert.
    insertion = event()
    tray.update(exists=True, state=3, tray_type="PLA", tray_color=chosen.rgba)

    @asynccontextmanager
    async def session():
        yield db_session

    main._stock_insertion_seen.clear()
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main, "printer_state_to_dict", return_value={}),
        patch.object(main, "_repeat_available_for", AsyncMock(return_value=False)),
        patch.object(main.ws_manager, "broadcast", AsyncMock()),
        patch.object(main.ws_manager, "send_printer_status", AsyncMock()),
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", AsyncMock()),
        patch("backend.app.services.filament_low.check_printer", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True)),
        patch("backend.app.api.routes.inventory.tray_types_written_for", AsyncMock(return_value={"PLA"})),
    ):
        # This is the actual callback order in _handle_ams_data.
        await main.on_ams_change(p.id, state.raw_data["ams"])
        await main.on_stock_spool_inserted(p.id, insertion)
    assigned = (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one()
    rows = (await db_session.execute(select(PrintUsageEvent).order_by(PrintUsageEvent.id))).scalars().all()
    assert [(r.event, r.spool_id) for r in rows] == (
        [(EVENT_RUNOUT, old.id)] if same_spool else [(EVENT_RUNOUT, old.id), (EVENT_SPOOL_LOADED, replacement.id)]
    )
    db_session.add(Settings(key="runout_archive_spool_enabled", value="true"))
    await db_session.commit()
    with patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        await apply_runout_zero_corrections(db_session, p.id, rows, 0.0)
    await db_session.refresh(old)
    print(
        {
            "manual_same_spool": same_spool,
            "manual_while_empty": preassigned_while_empty,
            "assigned": assigned,
            "chosen": chosen.id,
            "journal": [(r.event, r.spool_id) for r in rows],
            "old_weight_used": old.weight_used,
            "old_archived": old.archived_at is not None,
        }
    )
    assert assigned == chosen.id, [(r.event, r.spool_id) for r in rows]
    if same_spool:
        assert old.weight_used == 700
        assert old.archived_at is None
    else:
        assert old.weight_used == 1000
        assert old.archived_at is not None

    if later_runout_kind is None:
        return

    # The real SQLite journal has second-resolution server timestamps. Let a
    # later episode happen after the manual API write rather than rewriting
    # either that timestamp or the reconciled fingerprint in the fixture.
    await asyncio.sleep(1.1)
    next_spool = replacement if same_spool else await spool(db_session)
    state.layer_num = 280
    for event_name, kind in ((EVENT_RESUME, None), (EVENT_RUNOUT, later_runout_kind)):
        await record_event(
            db_session,
            printer_id=p.id,
            archive_id=archive.id,
            layer_num=280,
            event=event_name,
            kind=kind,
            global_tray_id=0,
            spool_id=chosen.id,
        )
    next_insertion = {**event(), "sequence": 2}
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main, "printer_state_to_dict", return_value={}),
        patch.object(main, "_repeat_available_for", AsyncMock(return_value=False)),
        patch.object(main.ws_manager, "broadcast", AsyncMock()),
        patch.object(main.ws_manager, "send_printer_status", AsyncMock()),
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", AsyncMock()),
        patch("backend.app.services.filament_low.check_printer", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True)),
        patch("backend.app.api.routes.inventory.tray_types_written_for", AsyncMock(return_value={"PLA"})),
    ):
        await main.on_ams_change(p.id, state.raw_data["ams"])
        await main.on_stock_spool_inserted(p.id, next_insertion)
        await main.on_stock_spool_inserted(p.id, next_insertion)  # duplicate is harmless
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == next_spool.id
    rows = (await db_session.execute(select(PrintUsageEvent).order_by(PrintUsageEvent.id))).scalars().all()
    assert [(r.spool_id, r.layer_num) for r in rows if r.event == EVENT_SPOOL_LOADED] == (
        [(replacement.id, 280)] if same_spool else [(replacement.id, 140), (next_spool.id, 280)]
    )
    with patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        await apply_runout_zero_corrections(db_session, p.id, rows, 0.0)
    await db_session.refresh(chosen)
    assert chosen.weight_used == chosen.label_weight
    assert chosen.archived_at is not None
