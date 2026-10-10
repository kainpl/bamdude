"""Synthetic external runout → user resume; no real printer or production DB."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.print_usage_event import (
    EVENT_RESUME,
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    KIND_AMBIGUOUS,
    KIND_EXTERNAL,
    PrintUsageEvent,
)
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.auto_stock_spool import claim_on_external_resume
from backend.app.services.print_usage_journal import record_event
from backend.tests.unit.services.test_auto_stock_spool import GROUP, printer, spool
from backend.tests.unit.services.test_usage_tracker_runout import _journal, _make_archive


async def setup(db, factory, tmp_path, slot=0):
    p = await printer(factory)
    old = await spool(db, weight_used=800, last_used=datetime.now(timezone.utc).replace(tzinfo=None))
    new = await spool(db)
    db.add(
        SpoolAssignment(
            printer_id=p.id,
            ams_id=255,
            tray_id=slot,
            spool_id=old.id,
            fingerprint_color=old.rgba,
            fingerprint_type=old.material,
        )
    )
    await db.commit()
    archive = await _make_archive(db, p, tmp_path)
    await _journal(db, p, archive, [(EVENT_RUNOUT, KIND_EXTERNAL, 254 + slot, 140, old.id)])
    tray = {"id": 254 + slot, "tray_type": "PLA", "tray_color": old.rgba, "tray_uuid": "", "tag_uid": ""}
    state = SimpleNamespace(
        connected=True,
        connection_generation=1,
        state="RUNNING",
        layer_num=143,
        tray_now=254 + slot,
        h2d_extruder_snow={0: 255} if slot else {},
        raw_data={"vt_tray": [tray], "ams": []},
        subtask_name="Product A",
        gcode_file="",
        pause_reason=None,
        pause_reason_label=None,
        pause_started_at=None,
    )
    pm = SimpleNamespace(get_status=lambda _: state)
    event = {"generation": 1, "observed_at": datetime.now(timezone.utc).replace(tzinfo=None)}
    return p, old, new, archive, pm, state, event


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("slot", [0, 1])
@pytest.mark.parametrize("label,consumed", [(1000, 800), (3000, 200)])
async def test_external_resume_replaces_once_and_closes_old_balance(
    db_session, printer_factory, tmp_path, slot, label, consumed
):
    from backend.app.services.print_usage_journal import load_events
    from backend.app.services.usage_tracker import apply_runout_zero_corrections

    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path, slot)
    p.ams_policies = {"auto_stock_spool": {"enabled": True, "group": {**GROUP, "label_weight": label}}}
    old.label_weight = new.label_weight = label
    old.weight_used = consumed
    await db_session.commit()
    outcome = await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm)
    assert outcome == {"reason": "assigned", "spool_id": new.id, "ams_id": 255, "tray_id": slot}
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == new.id
    events = await load_events(db_session, p.id, archive.id)
    assert [(e.event, e.spool_id, e.layer_num) for e in events] == [
        (EVENT_RUNOUT, old.id, 140),
        (EVENT_SPOOL_LOADED, new.id, 140),
    ]
    await apply_runout_zero_corrections(db_session, p.id, events, 0)
    await db_session.refresh(old)
    assert old.weight_used == label
    assert new.weight_used == 0
    assert (await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm))[
        "reason"
    ] == "no_unambiguous_external_runout"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    [
        "ordinary_pause",
        "jam",
        "unbound",
        "manual_new",
        "manual_same",
        "resumed_before",
        "no_stock",
        "policy_off",
        "disconnected",
        "stale",
        "reconnect",
        "not_running",
        "ams_feed",
        "unknown_feed",
        "right_unloaded_sentinel",
        "finished",
        "two_runouts",
        "rfid",
        "wrong_group",
        "no_assignment",
        "missing_vt",
        "unknown_profile",
        "outgoing_wrong_group",
        "spoolman",
        "archived_printer",
        "inactive_printer",
    ],
)
async def test_external_resume_does_not_guess(db_session, printer_factory, tmp_path, case):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    runout = (await db_session.execute(select(PrintUsageEvent))).scalar_one()
    if case == "ordinary_pause":
        await db_session.delete(runout)
    elif case == "jam":
        runout.kind = KIND_AMBIGUOUS
    elif case == "unbound":
        runout.spool_id = None
    elif case in ("manual_new", "manual_same"):
        assignment = (await db_session.execute(select(SpoolAssignment))).scalar_one()
        assignment.created_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
        if case == "manual_new":
            assignment.spool_id = new.id
    elif case == "resumed_before":
        await record_event(db_session, printer_id=p.id, archive_id=archive.id, layer_num=140, event=EVENT_RESUME)
    elif case == "no_stock":
        await db_session.delete(new)
    elif case == "policy_off":
        p.ams_policies = {"auto_stock_spool": {"enabled": False}}
    elif case == "disconnected":
        state.connected = False
    elif case == "stale":
        event["observed_at"] -= timedelta(seconds=31)
    elif case == "reconnect":
        state.connection_generation = 2
    elif case == "not_running":
        state.state = "PAUSE"
    elif case == "ams_feed":
        state.tray_now = 0
    elif case == "unknown_feed":
        state.tray_now = 255
    elif case == "right_unloaded_sentinel":
        runout.global_tray_id = 255
        state.tray_now = 255
        state.raw_data["vt_tray"][0]["id"] = 255
    elif case == "finished":
        archive.status = "completed"
    elif case == "two_runouts":
        await _journal(db_session, p, archive, [(EVENT_RUNOUT, KIND_EXTERNAL, 255, 140, old.id)])
    elif case == "rfid":
        state.raw_data["vt_tray"][0]["tag_uid"] = "1234567890ABCDEF"
    elif case == "wrong_group":
        new.rgba = "0000FFFF"
    elif case == "no_assignment":
        await db_session.delete((await db_session.execute(select(SpoolAssignment))).scalar_one())
    elif case == "missing_vt":
        state.raw_data["vt_tray"] = []
    elif case == "unknown_profile":
        state.raw_data["vt_tray"][0]["tray_type"] = ""
    elif case == "outgoing_wrong_group":
        old.rgba = "0000FFFF"
    elif case == "spoolman":
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="spoolman_enabled", value="true"))
    elif case == "archived_printer":
        p.archived = True
    elif case == "inactive_printer":
        p.is_active = False
    await db_session.commit()
    outcome = await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm)
    assert outcome["reason"] != "assigned"
    assignment = (await db_session.execute(select(SpoolAssignment))).scalar_one_or_none()
    if case == "no_assignment":
        assert assignment is None
    else:
        assert assignment.spool_id == (new.id if case == "manual_new" else old.id)
    assert (
        not (await db_session.execute(select(PrintUsageEvent).where(PrintUsageEvent.event == EVENT_SPOOL_LOADED)))
        .scalars()
        .all()
    )
    assert old.weight_used == 800


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resume_callback_uses_current_runout_without_sending_printer_commands(
    db_session, printer_factory, tmp_path
):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)

    @asynccontextmanager
    async def session():
        yield db_session

    publisher = AsyncMock()
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main.printer_manager, "get_printer", return_value=SimpleNamespace(name="Product A printer")),
        patch.object(main.ws_manager, "broadcast", AsyncMock()),
        patch.object(main.ws_manager, "send_print_resumed", AsyncMock()),
        patch.object(main.notification_service, "on_print_resume", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", publisher),
    ):
        await asyncio.gather(
            main._handle_resume_edge(p.id, state, stock_resume_witnessed=True),
            main._handle_resume_edge(p.id, state, stock_resume_witnessed=True),
        )
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == new.id
    events = (await db_session.execute(select(PrintUsageEvent).order_by(PrintUsageEvent.id))).scalars().all()
    assert sum(e.event == EVENT_SPOOL_LOADED for e in events) == 1
    assert events[-1].event == EVENT_RESUME and events[-1].spool_id == new.id
    publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("change", ["reconnect", "feed_changed", "finished"])
async def test_external_resume_rechecks_live_state_after_stock_lookup(db_session, printer_factory, tmp_path, change):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    calls = 0

    def current(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            if change == "reconnect":
                state.connection_generation = 2
            elif change == "feed_changed":
                state.tray_now = 0
            else:
                state.state = "FINISH"
        return state

    pm.get_status = current
    outcome = await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm)
    assert outcome["reason"] != "assigned"
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == old.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_assignment_and_journal_boundary_rollback_together(db_session, printer_factory, tmp_path):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    old_id = old.id
    with (
        patch(
            "backend.app.services.print_usage_journal.note_assignment_change",
            AsyncMock(side_effect=RuntimeError("synthetic journal failure")),
        ),
        pytest.raises(RuntimeError, match="synthetic journal failure"),
    ):
        await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm)
    await db_session.rollback()
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == old_id
    assert (
        not (await db_session.execute(select(PrintUsageEvent).where(PrintUsageEvent.event == EVENT_SPOOL_LOADED)))
        .scalars()
        .all()
    )
