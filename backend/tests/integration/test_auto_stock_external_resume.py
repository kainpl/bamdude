"""Synthetic external runout → user resume; no real printer or production DB."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
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
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.print_usage_journal import load_events, record_event
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


def _report_p2s_feed(state, *, snow=0xFF00, flags=2, command="push_status", ams_units=()):
    # Synthetic report of the observed P2S field values, not a runout capture.
    client = BambuMQTTClient(ip_address="192.0.2.1", serial_number="TESTP2S", access_code="12345678")
    client.state.connection_generation = state.connection_generation
    report = {
        "command": command,
        "device": {"extruder": {"info": [{"id": 0, "snow": snow, "info": flags}]}},
    }
    if ams_units is not None:
        report["ams"] = {"ams": list(ams_units)}
    client.state.feed_telemetry.observe({"print": report}, "P2S")
    client._parse_extruder_slots(report)
    state.tray_now = 0
    state.extruder_slots = client.state.extruder_slots
    state.feed_telemetry = client.state.feed_telemetry


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    [
        "external",
        "ams_slot",
        "unloaded",
        "empty_sentinel",
        "command_ack",
        "before_runout",
        "stale",
        "previous_connection",
        "ams_present",
        "ams_unknown",
        "other_model",
        "feed_changed_during_claim",
    ],
)
async def test_p2s_tray_zero_external_resume(db_session, printer_factory, tmp_path, case):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    event["printer_model"] = "P1S" if case == "other_model" else "P2S"
    _report_p2s_feed(
        state,
        snow={"ams_slot": 0, "empty_sentinel": 0xFFFF}.get(case, 0xFF00),
        flags=0 if case == "unloaded" else 2,
        command="project_file" if case == "command_ack" else "push_status",
        ams_units={"ams_present": ({"id": 0},), "ams_unknown": None}.get(case, ()),
    )
    if case == "command_ack":
        # Keep the independently established absence of an AMS. Only the
        # source observation itself is from a command echo.
        state.feed_telemetry.observe({"print": {"ams": {"ams": []}}}, "P2S")
    if case in ("before_runout", "stale", "previous_connection"):
        runout = (await load_events(db_session, p.id, archive.id))[0]
        generation, observed_at = state.extruder_slots[0].status_observation
        if case == "before_runout":
            observed_at = runout.created_at - timedelta(microseconds=1)
        elif case == "stale":
            observed_at -= timedelta(seconds=31)
            runout.created_at -= timedelta(seconds=60)
            await db_session.commit()
        else:
            generation -= 1
        state.extruder_slots[0] = replace(state.extruder_slots[0], status_observation=(generation, observed_at))
    if case == "feed_changed_during_claim":
        reads = 0

        def current(_):
            nonlocal reads
            reads += 1
            if reads == 3:
                _report_p2s_feed(state, flags=0)
            return state

        pm.get_status = current

    outcome = await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm)
    if case == "external":
        assert outcome == {"reason": "assigned", "spool_id": new.id, "ams_id": 255, "tray_id": 0}
        assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == new.id
        events = await load_events(db_session, p.id, archive.id)
        assert [(e.event, e.global_tray_id, e.spool_id, e.layer_num) for e in events] == [
            (EVENT_RUNOUT, 254, old.id, 140),
            (EVENT_SPOOL_LOADED, 254, new.id, 140),
        ]
        assert (await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm))[
            "reason"
        ] == "no_unambiguous_external_runout"
    else:
        expected = "presence_unknown" if case == "feed_changed_during_claim" else "external_feed_not_confirmed"
        assert outcome["reason"] == expected
        assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == old.id
        assert [e.event for e in await load_events(db_session, p.id, archive.id)] == [EVENT_RUNOUT]


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
@pytest.mark.parametrize("model", ["P1S", "P2S"])
async def test_resume_callback_uses_current_runout_without_sending_printer_commands(
    db_session, printer_factory, tmp_path, model
):
    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    if model == "P2S":
        _report_p2s_feed(state)

    @asynccontextmanager
    async def session():
        yield db_session

    publisher = AsyncMock()
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main.printer_manager, "get_printer", return_value=SimpleNamespace(name="Product A printer")),
        patch.object(main.printer_manager, "effective_model_for", return_value=model),
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
    first_resume = next(e for e in events if e.event == EVENT_RESUME)
    assert (first_resume.global_tray_id, first_resume.spool_id) == (254, new.id)
    publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("first_tray", [255, 254])
async def test_generic_p1s_runout_gains_source_proof_then_claims_once(
    db_session, printer_factory, tmp_path, first_tray
):
    from sqlalchemy import delete

    from backend.app.services.print_usage_journal import record_runout
    from backend.tests.unit.services.test_external_stock_generic_runout import client

    p, old, new, archive, pm, state, event = await setup(db_session, printer_factory, tmp_path)
    await db_session.execute(delete(PrintUsageEvent))
    await db_session.commit()
    mqtt = client()
    state.layer_num = 160
    mqtt.state.feed_telemetry.ams_known = False
    mqtt.state.tray_now = first_tray
    mqtt._detect_runout_signals()
    _, kind, tray, layer = mqtt.on_usage_event.call_args.args
    await record_runout(
        db_session,
        printer_id=p.id,
        archive_id=archive.id,
        layer_num=layer,
        kind=kind,
        global_tray_id=tray,
        spool_id=old.id if tray is not None else None,
    )
    await db_session.commit()
    assert (await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm))["reason"] == (
        "no_unambiguous_external_runout"
    )

    mqtt.state.feed_telemetry.ams_known = True
    mqtt.state.tray_now = 254
    mqtt._detect_runout_signals()
    _, kind, tray, layer = mqtt.on_usage_event.call_args.args
    assert (kind, tray) == (KIND_EXTERNAL, 254)
    await record_runout(
        db_session,
        printer_id=p.id,
        archive_id=archive.id,
        layer_num=layer,
        kind=kind,
        global_tray_id=tray,
        spool_id=old.id,
    )
    await db_session.commit()
    runouts = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
    # A tray-less sighting remains forensic history; only the later positively
    # named holder is eligible for a claim. A known-tray sighting upgrades in place.
    assert len(runouts) == (2 if first_tray == 255 else 1)
    named = [row for row in runouts if row.global_tray_id == 254]
    assert len(named) == 1
    assert (named[0].kind, named[0].spool_id) == (KIND_EXTERNAL, old.id)
    assert (await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm))["spool_id"] == new.id
    assert (await claim_on_external_resume(db_session, printer_id=p.id, event=event, manager=pm))["reason"] == (
        "no_unambiguous_external_runout"
    )


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
