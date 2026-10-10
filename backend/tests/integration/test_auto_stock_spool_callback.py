"""Insertion callback deduplication, configuration failures and existing AMS overlay."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_advertised_overlay as overlay
from backend.tests.unit.services.test_auto_stock_spool import event, manager, printer, spool


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("later_change", [None, "color", "rfid", "removal"])
async def test_unknown_insertion_then_default_metadata_keeps_one_spool_until_config_echo(
    db_session, printer_factory, later_change
):
    p = await printer(printer_factory)
    first = await spool(db_session)
    await spool(db_session)
    pm, state, tray = manager(slot=1)
    state.state = "IDLE"
    tray.update(state=3, tray_type="", tray_color="00000000")

    @asynccontextmanager
    async def session():
        yield db_session

    publisher = AsyncMock(return_value=True)
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main, "printer_state_to_dict", return_value={}),
        patch.object(main, "_repeat_available_for", AsyncMock(return_value=False)),
        patch.object(main.ws_manager, "broadcast", AsyncMock()),
        patch.object(main.ws_manager, "send_printer_status", AsyncMock()),
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", AsyncMock()),
        patch("backend.app.services.filament_low.check_printer", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", publisher),
        patch("backend.app.api.routes.inventory.tray_types_written_for", AsyncMock(return_value={"PLA"})),
    ):
        insertion = event(slot=1)
        await main.on_stock_spool_inserted(p.id, insertion)
        # Firmware announces presence before it knows the material, then sends
        # its temporary white default after accepting our configuration.
        await main.on_ams_change(p.id, state.raw_data["ams"])
        tray.update(tray_type="PLA", tray_color="FFFFFFFF", tray_info_idx="GFA00")
        await main.on_ams_change(p.id, state.raw_data["ams"])
        rows = (await db_session.execute(select(SpoolAssignment))).scalars().all()
        assert [a.spool_id for a in rows] == [first.id]
        tray.update(tray_color=first.rgba)
        await main.on_ams_change(p.id, state.raw_data["ams"])
        await main.on_stock_spool_inserted(p.id, insertion)
        rows = (await db_session.execute(select(SpoolAssignment))).scalars().all()
        assert [a.spool_id for a in rows] == [first.id]
        assert rows[0].fingerprint_color == first.rgba
        assert rows[0].fingerprint_type == "PLA"
        if later_change == "color":
            tray["tray_color"] = "0000FFFF"
        elif later_change == "rfid":
            tray.update(tray_uuid="A" * 32, tag_uid="1234567890ABCDEF")
        elif later_change == "removal":
            tray.update(exists=False, state=9, tray_type="", tray_color="")
        if later_change:
            await main.on_ams_change(p.id, state.raw_data["ams"])
            assert (await db_session.execute(select(SpoolAssignment))).scalars().all() == []


@pytest.fixture(autouse=True)
def clear_callback_memory():
    main._stock_insertion_seen.clear()
    main._ams_assignment_locks.clear()
    yield
    main._stock_insertion_seen.clear()
    main._ams_assignment_locks.clear()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("published", [True, False, RuntimeError("synthetic publish failure")])
async def test_callback_claims_once_and_reports_failed_configuration(db_session, printer_factory, published):
    p = await printer(printer_factory)
    s = await spool(db_session)
    await spool(db_session)
    pm, state, _ = manager()
    e = event()

    @asynccontextmanager
    async def session():
        try:
            yield db_session
        except BaseException:
            await db_session.rollback()
            raise

    publisher = (
        AsyncMock(side_effect=published) if isinstance(published, Exception) else AsyncMock(return_value=published)
    )
    broadcast = AsyncMock()
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main.ws_manager, "broadcast", broadcast),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", publisher),
        patch.object(overlay, "forget") as forget,
    ):
        await main.on_stock_spool_inserted(p.id, e)
        await main.on_stock_spool_inserted(p.id, e)
    assignments = (await db_session.execute(select(SpoolAssignment))).scalars().all()
    assert [a.spool_id for a in assignments] == [s.id]
    publisher.assert_awaited_once()
    forget.assert_called_once_with(p.id, 0, 0)
    types = [call.args[0]["type"] for call in broadcast.call_args_list]
    assert types.count("spool_auto_assigned") == 1
    assert ("stock_spool_config_failed" in types) is (published is not True)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("printer_state", ["IDLE", "RUNNING", "PAUSE"])
@pytest.mark.parametrize("returned_report", ["same", "blank", "reset_color", "restart", "missing_tray", "runout"])
async def test_partial_spool_return_preserves_identity_except_confirmed_runout(
    db_session, printer_factory, tmp_path, printer_state, returned_report
):
    from backend.tests.unit.services.test_auto_stock_spool import client

    p = await printer(printer_factory)
    partial = await spool(db_session, weight_used=300)
    full = await spool(db_session, added_full=None)
    db_session.add(
        SpoolAssignment(
            spool_id=partial.id,
            printer_id=p.id,
            ams_id=0,
            tray_id=0,
            fingerprint_color=partial.rgba,
            fingerprint_type=partial.material,
        )
    )
    await db_session.commit()
    if returned_report == "runout":
        from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_PAUSE
        from backend.tests.unit.services.test_usage_tracker_runout import _journal, _make_archive

        archive = await _make_archive(db_session, p, tmp_path)
        await _journal(db_session, p, archive, [(EVENT_RUNOUT, KIND_PAUSE, 0, 140, partial.id)])
    pm, state, _ = manager()
    state.state = printer_state
    occupied = state.raw_data["ams"]
    empty = [{"id": 0, "tray": [{"id": 0, "exists": False, "state": 9, "tray_type": "", "tray_color": ""}]}]
    detector = client()
    assert detector._stock_spool_insertions(occupied, {"tray_exist_bits": "1"}) == []

    @asynccontextmanager
    async def session():
        yield db_session

    publisher = AsyncMock(return_value=True)
    with (
        patch.object(main, "async_session", session),
        patch.object(main.printer_manager, "get_status", pm.get_status),
        patch.object(main, "printer_state_to_dict", return_value={}),
        patch.object(main, "_repeat_available_for", AsyncMock(return_value=False)),
        patch.object(main.ws_manager, "broadcast", AsyncMock()),
        patch.object(main.ws_manager, "send_printer_status", AsyncMock()),
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", AsyncMock()),
        patch("backend.app.services.filament_low.check_printer", AsyncMock()),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", publisher),
    ):
        state.raw_data["ams"] = [] if returned_report == "missing_tray" else empty
        assert detector._stock_spool_insertions(empty, {"tray_exist_bits": "0"}) == []
        await main.on_ams_change(p.id, state.raw_data["ams"])  # real empty-slot auto-unlink path
        if returned_report == "restart":
            # Restart loses detector memory, but not the persistent assignment.
            detector = client()
            main._stock_insertion_seen.clear()
            main._ams_assignment_locks.clear()
        state.raw_data["ams"] = occupied
        if returned_report == "blank":
            occupied[0]["tray"][0].update(tray_type="", tray_color="", state=9)
        elif returned_report == "reset_color":
            occupied[0]["tray"][0]["tray_color"] = "000000FF"
        insertions = detector._stock_spool_insertions(occupied, {"tray_exist_bits": "1"})
        assert len(insertions) == (0 if returned_report == "restart" else 1)
        await main.on_ams_change(p.id, occupied)
        for insertion in insertions:
            await main.on_stock_spool_inserted(p.id, insertion)
    assigned = (await db_session.execute(select(SpoolAssignment))).scalars().all()
    if returned_report == "runout":
        # Operator contract: a confirmed runout means a new full replacement.
        assert [a.spool_id for a in assigned] == [full.id]
        publisher.assert_awaited_once()
    else:
        assert [a.spool_id for a in assigned] == [partial.id]
        publisher.assert_not_awaited()
    await db_session.refresh(partial)
    await db_session.refresh(full)
    assert partial.weight_used == 300 and full.weight_used == 0


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("owner", ["policy_off", "spoolman", "rfid", "external"])
async def test_partial_return_guard_does_not_override_other_assignment_owners(
    db_session, printer_factory, monkeypatch, owner
):
    from backend.tests.integration.test_ams_unlink_runout_guard import _run_on_ams_change

    p = await printer(printer_factory)
    partial = await spool(db_session, weight_used=300)
    if owner == "policy_off":
        p.ams_policies = {}
    if owner == "spoolman":
        monkeypatch.setattr("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true"))
    ams_id = 255 if owner == "external" else 0
    db_session.add(
        SpoolAssignment(
            spool_id=partial.id,
            printer_id=p.id,
            ams_id=ams_id,
            tray_id=0,
            fingerprint_color=partial.rgba,
            fingerprint_type=partial.material,
        )
    )
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(main, "async_session", session)
    if owner == "rfid":
        # A different Bambu RFID spool must still release the previous identity.
        report = [{"id": 0, "tray": [{"id": 0, "tray_uuid": "A" * 32, "tag_uid": "1234567890ABCDEF"}]}]
    elif owner == "external":
        # The existing external path is covered via a present, changed vt_tray.
        from types import SimpleNamespace

        status = SimpleNamespace(
            state="IDLE", raw_data={"vt_tray": [{"id": 254, "tray_type": "PETG", "tray_color": "000000FF"}]}
        )
        with (
            patch.object(main.printer_manager, "get_status", return_value=status),
            patch.object(main.ws_manager, "send_printer_status", AsyncMock()),
            patch.object(main.ws_manager, "broadcast", AsyncMock()),
        ):
            await main.on_ams_change(p.id, [])
        assert (await db_session.execute(select(SpoolAssignment))).scalars().all() == []
        return
    else:
        report = [{"id": 0, "tray": [{"id": 0, "state": 9, "tray_type": "", "tray_color": ""}]}]
    await _run_on_ams_change(p.id, report, "IDLE")
    assert (await db_session.execute(select(SpoolAssignment))).scalars().all() == []
