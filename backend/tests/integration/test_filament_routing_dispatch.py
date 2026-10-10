"""Synthetic files, real intake/distributor/preflight/MQTT serialization; no printer I/O."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.macro import Macro
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user_filament import UserFilamentFamily
from backend.app.schemas.print_options_preference import PrintOptionsPreferenceData
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.filament_deferred import defer_claim
from backend.app.services.filament_intake import read_item_requirements
from backend.app.services.filament_policy import deserialize_policy, queue_policy
from backend.app.services.filament_preflight import final_guard, preflight_item, settle_plan
from backend.app.services.filament_routing import RoutingDeferred, fingerprint
from backend.app.services.printer_feed_snapshot import FeedTelemetry
from backend.app.services.printer_manager import printer_manager
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = pytest.mark.integration


async def setup_source(db, tmp_path, printer_factory, monkeypatch):
    path = write_routing_3mf(
        tmp_path / "part.gcode.3mf",
        {
            15: [
                {"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"},
            ]
        },
    )
    source = LibraryFile(filename=path.name, file_path=str(path), file_size=path.stat().st_size, file_type="gcode")
    db.add(source)
    printer = await printer_factory(model="P1P")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, auto_distribute_eligible=True)
    db.add(queue)
    await db.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
    mqtt._client = MagicMock()
    mqtt.state.connected = True
    mqtt.state.state = "IDLE"
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "ams": {"ams": []},
                "vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "0000FF"},
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, "P1P")
    return source, printer, queue, mqtt


@pytest.mark.parametrize("force,expect_assignment", [(False, True), (True, False)])
async def test_auto_intake_tick_and_publish_sparse_external(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, force, expect_assignment
):
    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    response = await committing_client.post(
        "/api/v1/auto-queue/",
        json={
            "library_file_id": source.id,
            "force_color_match": force,
        },
    )
    assert response.status_code == 200, response.text

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    item = (await db_session.execute(select(PrintQueueItem))).scalar_one_or_none()
    assert (item is not None) == expect_assignment
    if item is None:
        auto = (await db_session.execute(select(AutoQueueItem))).scalar_one()
        assert auto.waiting_reason
        mqtt._client.publish.assert_not_called()
        return
    assert item.plate_id == 15
    # m173: the promotion carries the captured source onto the per-printer row —
    # the assignment re-reads nothing from the share, and both rows own the blob
    # until the shared cleanup (queue-source-spool spec §7).
    auto_row = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    assert auto_row.queue_source_id is not None
    assert item.queue_source_id == auto_row.queue_source_id
    assert item.source_snapshot == auto_row.source_snapshot
    assert not item.use_ams
    assert json.loads(item.ams_mapping) == [-1, -1, 254]
    assert deserialize_policy(item.filament_routing).mode == "auto"
    # With no operator or system profile, promotion uses the concrete queue's
    # defaults — and a non-Swap target never inherits AutoQueue's old default.
    assert item.bed_levelling_mode == "on" and item.flow_cali_mode == "on"
    assert item.nozzle_offset_cali_mode == "on"
    assert item.execute_swap_macros is False and item.selected_macro_ids is None
    guard = await preflight_item(db_session, item, printer.id)
    guard = await final_guard(guard, printer.id)
    assert printer_manager.start_print(
        printer.id, source.filename, 15, ams_mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, routing_guard=guard
    )
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert command["param"] == "Metadata/plate_15.gcode"
    assert command["use_ams"] is False
    assert command["ams_mapping"] == [0, 0, 0]  # existing no-AMS wire convention
    assert "ams_mapping2" not in command


@pytest.mark.parametrize(
    ("swap_mode_enabled", "source_has_baked_swap_macros", "expect_swap_macros"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
async def test_auto_promotion_uses_the_target_model_profile(
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
    swap_mode_enabled,
    source_has_baked_swap_macros,
    expect_swap_macros,
):
    """A mixed-model auto row is configured only after its winner is known.

    This is the path that makes a P1S-only light macro arrive after Auto Queue
    promotion. Swap settings remain subject to the target printer and source
    safety gates, rather than merely being present in the saved profile.
    """
    source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    printer.model = "P1S"
    printer.swap_mode_enabled = swap_mode_enabled
    source.swap_compatible = source_has_baked_swap_macros
    await db_session.commit()

    response = await committing_client.post(
        "/api/v1/auto-queue/", json={"library_file_id": source.id, "target_model": "P1S"}
    )
    assert response.status_code == 200, response.text
    auto = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    assert auto.created_by_id is not None

    light = Macro(name="P1S light", event="print_started", gcode="M355 S1", printer_models='["P1S"]')
    db_session.add(light)
    await db_session.flush()
    db_session.add(
        PrintOptionsPreference(
            user_id=auto.created_by_id,
            printer_model="P1S",
            options=PrintOptionsPreferenceData.model_validate(
                {
                    "print_options": {
                        "bed_levelling": "auto",
                        "flow_cali": "off",
                        "layer_inspect": True,
                        "timelapse": True,
                        "timelapse_storage": "external",
                        "mesh_mode_fast_check": False,
                        "gcode_injection": True,
                        "nozzle_offset_cali": "off",
                    },
                    "swap_macros": {"execute": True, "events": ["swap_mode_start"]},
                    "event_macros": {"deselected_ids": []},
                }
            ).model_dump(),
        )
    )
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()

    item = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    assert item.bed_levelling is False and item.bed_levelling_mode == "auto"
    assert item.flow_cali is False and item.flow_cali_mode == "off"
    assert item.layer_inspect is True
    assert item.timelapse is True and item.timelapse_storage == "external"
    assert item.mesh_mode_fast_check is False
    assert item.gcode_injection is True
    assert item.nozzle_offset_cali is False and item.nozzle_offset_cali_mode == "off"
    assert json.loads(item.selected_macro_ids) == [light.id]
    assert item.execute_swap_macros is expect_swap_macros
    if expect_swap_macros:
        assert json.loads(item.swap_macro_events) == ["swap_mode_start"]
    else:
        assert item.swap_macro_events is None


async def test_publish_boundary_catches_change_after_final_preflight(
    db_session, tmp_path, printer_factory, monkeypatch
):
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    routing, plate = await prepare_routing(db_session, printer_id=printer.id, library_file_id=source.id)
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
    # The boundary asks the plan's own slot under the channel rule, so it names
    # what is wrong with it rather than "something changed".
    with pytest.raises(RoutingDeferred, match="material_mismatch"):
        printer_manager.start_print(
            printer.id,
            source.filename,
            plate,
            ams_mapping=guard.plan.mapping,
            use_ams=guard.plan.use_ams,
            routing_guard=guard,
        )
    mqtt._client.publish.assert_not_called()


async def test_per_printer_routing_matches_the_base_material_when_enabled(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """A plate sliced against a foreign preset still routes onto the same material.

    The captured routing survives all the way to preflight: PETG on both sides,
    two preset ids that will never agree, and an operator who said «any PETG
    will do». The catalogue can name this id — and that changes nothing either
    way; the plate's own declared material is what was compared.
    """
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    path = write_routing_3mf(
        tmp_path / source.filename,
        {15: [{"id": 3, "type": "PETG", "tray_info_idx": "P333PETG", "used_g": "0.0001"}]},
    )
    source.file_path = str(path)
    db_session.add(
        UserFilamentFamily(
            filament_id="P333PETG",
            ecosystem="bambu",
            alias="333Print PETG",
            vendor="333Print",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    mqtt._process_message(
        {"print": {"vt_tray": {"id": 254, "tray_type": "PETG", "tray_color": "FF0000", "tray_info_idx": "GFG99"}}}
    )
    await db_session.commit()

    routing, plate = await prepare_routing(
        db_session,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"allow_base_material_match": True},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan is not None


@pytest.mark.parametrize("race", ["none", "cancel", "reclaim", "delete"])
async def test_defer_cas_never_revives_another_attempt(db_session, printer_factory, race):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    started = datetime(2026, 9, 10, 10)
    item = PrintQueueItem(queue_id=queue.id, status="printing", started_at=started, archive_id=None)
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    iid = item.id
    if race == "cancel":
        item.status = "cancelled"
    if race == "reclaim":
        item.started_at = started + timedelta(seconds=1)
    if race == "delete":
        queue.current_item_id = None
        await db_session.delete(item)
    await db_session.commit()
    restored = await defer_claim(db_session, item_id=iid, started_at=started, reason="feed_state_changed")
    await db_session.commit()
    assert restored == (race == "none")
    if race != "delete":
        await db_session.refresh(item)
        assert item.status == {"none": "pending", "cancel": "cancelled", "reclaim": "printing"}[race]


@pytest.mark.parametrize("model", ["H2D", "O1D", "H2D Pro", "H2DPRO", "H2C", "O1C", "X2D", "N6", "O1E", "O2D", "O1C2"])
@pytest.mark.parametrize("kind", ["mixed", "external"])
async def test_dual_topology_and_real_mqtt_wire(db_session, tmp_path, printer_factory, monkeypatch, model, kind):
    from backend.app.services.filament_policy_write import prepare_routing
    from backend.app.utils.printer_models import normalize_model_name
    from backend.tests.fixtures.filament_routing_cases import DUAL_SETTINGS, mixed_filaments

    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    path = write_routing_3mf(tmp_path / "dual.3mf", {2: mixed_filaments()}, model=model, settings=DUAL_SETTINGS)
    source.file_path, source.filename, printer.model = str(path), path.name, normalize_model_name(model)
    await db_session.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "DUAL_SYNTHETIC", "00000000", model=model)
    mqtt._client = MagicMock()
    mqtt.state.connected, mqtt.state.state = True, "IDLE"
    # Right carriage 0.6, left 0.4, as the file wants. On an H2C the right one is
    # the rack carriage (a dock stands in for it) and the left is the fixed
    # hotend, id 1 — BambuStudio's numbering, upstream 45dc139c.
    nozzle_ids = (16, 1) if normalize_model_name(model) == "H2C" else (0, 1)
    mqtt._process_message(
        {
            "print": {
                "ams": {
                    "ams": (
                        [{"id": 0, "info": "100", "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000"}]}]
                        if kind == "mixed"
                        else []
                    )
                },
                "vir_slot": [
                    {"id": 254, "tray_type": "PLA", "tray_color": "FF0000"},
                    {"id": 255, "tray_type": "PETG", "tray_color": "00FF00"},
                ],
                "device": {
                    "nozzle": {
                        "info": [{"id": nozzle_ids[0], "diameter": "0.6"}, {"id": nozzle_ids[1], "diameter": "0.4"}]
                    }
                },
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, printer.model)
    routing, plate = await prepare_routing(
        db_session,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"feed_policy": "auto" if kind == "mixed" else "external_only"},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    assert guard.plan.mapping == ([0, 255] if kind == "mixed" else [254, 255])
    assert printer_manager.start_print(
        printer.id, path.name, plate, ams_mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, routing_guard=guard
    )
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert command["use_ams"] is (kind == "mixed")
    assert command["ams_mapping"] == ([0, -1] if kind == "mixed" else [-1, -1])
    assert command["ams_mapping2"] == [
        {"ams_id": 0 if kind == "mixed" else 254, "slot_id": 0},
        {"ams_id": 255, "slot_id": 0},
    ]
    assert command["param"] == "Metadata/plate_2.gcode"


# --------------------------------------------------------------------------- #
# A profile re-tagged while a prepared job waited is not a changed feed
# --------------------------------------------------------------------------- #


async def a_routed_job(db, tmp_path, printer_factory, monkeypatch, *, allow_base_material_match=True):
    """One prepared job against one external spool that carries a profile id."""
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    mqtt._process_message(
        {
            "print": {
                "vt_tray": {
                    "id": 254,
                    "tray_type": "PLA",
                    "tray_color": "0000FF",
                    "tray_info_idx": "GFA00",
                    "tray_uuid": "THE-SPOOL",
                }
            }
        }
    )
    routing, plate = await prepare_routing(
        db,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"allow_base_material_match": allow_base_material_match},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db.add(item)
    await db.commit()
    return item, source, printer, plate, mqtt


def retag(mqtt, variant="GFB99"):
    """Only the profile id moves: the same spool, the same material, the same colour."""
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_info_idx": variant}}})


@pytest.mark.parametrize("when", ["before_final_guard", "after_final_guard"])
async def test_a_retagged_spool_no_longer_stops_a_prepared_job(
    db_session, tmp_path, printer_factory, monkeypatch, when
):
    """Re-profiling a spool moves no filament, so with the option on it moves no plan.

    Both boundaries the re-tag can land on: between preflight and the final
    refresh, and between that refresh and the synchronous guard at publish.
    """
    item, source, printer, plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    if when == "before_final_guard":
        retag(mqtt)
    guard = await final_guard(guard, printer.id)
    if when == "after_final_guard":
        retag(mqtt)
    assert printer_manager.start_print(
        printer.id,
        source.filename,
        plate,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )
    mqtt._client.publish.assert_called_once()


async def test_a_retag_does_not_stop_a_job_whose_file_names_no_profile(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """With the option off the operator asked for the FILE's profile — and this
    file names none, so a new profile id on the loaded spool asks nothing of it (П2)."""
    item, _source, printer, _plate, mqtt = await a_routed_job(
        db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match=False
    )
    guard = await preflight_item(db_session, item, printer.id)
    retag(mqtt)
    assert (await final_guard(guard, printer.id)).plan.mapping == guard.plan.mapping


def reconnect(mqtt):
    """What BambuMQTTClient does on a new session: a new generation AND an empty
    feed cache — together, always (``bambu_mqtt`` resets both in one place). The
    test this replaced bumped the generation alone, a state the client never produces."""
    mqtt.state.connection_generation += 1
    mqtt.state.feed_telemetry = FeedTelemetry()


def report_the_spool(mqtt, **change):
    """The printer's first full report on the new session — the same spool unless told otherwise."""
    tray = {
        "id": 254,
        "tray_type": "PLA",
        "tray_color": "0000FF",
        "tray_info_idx": "GFA00",
        "tray_uuid": "THE-SPOOL",
        **change,
    }
    mqtt._process_message({"print": {"command": "push_status", "ams": {"ams": []}, "vt_tray": tray}})


@pytest.mark.parametrize("allow_base_material_match", [True, False])
async def test_a_reconnect_that_reports_the_same_feed_keeps_the_prepared_job(
    db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match
):
    """Spec direct-print-silent-cancel §4.3 (A05): fresh evidence, same content — start."""
    item, source, printer, plate, mqtt = await a_routed_job(
        db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match=allow_base_material_match
    )
    guard = await preflight_item(db_session, item, printer.id)
    # The real pushall would publish through the same mocked client and spoil the
    # «published once» assertion below; the report it provokes is replayed by hand.
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    reconnect(mqtt)
    asyncio.get_running_loop().call_later(0.05, report_the_spool, mqtt)
    await settle_plan(guard, printer.id, timeout=2, poll=0.01)
    guard = await final_guard(guard, printer.id)
    assert printer_manager.start_print(
        printer.id,
        source.filename,
        plate,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )
    mqtt._client.publish.assert_called_once()


async def test_a_reconnect_that_reports_another_spool_defers(db_session, tmp_path, printer_factory, monkeypatch):
    """A06: the new session says something else is loaded — a new attempt, never a swapped plan."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    reconnect(mqtt)
    # Another tag on the same filament is the same plan now; another MATERIAL is not.
    report_the_spool(mqtt, tray_type="PETG")
    await settle_plan(guard, printer.id, timeout=2, poll=0.01, converge=0.05)
    with pytest.raises(RoutingDeferred, match="material_mismatch"):
        await final_guard(guard, printer.id)
    mqtt._client.publish.assert_not_called()


async def test_settle_plan_says_whether_the_session_changed(db_session, tmp_path, printer_factory, monkeypatch):
    """The runner re-binds the pre-start calibration only after a session change."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    assert await settle_plan(guard, printer.id, timeout=0.01, poll=0.01) is False
    reconnect(mqtt)
    report_the_spool(mqtt)
    assert await settle_plan(guard, printer.id, timeout=2, poll=0.01) is True


async def test_a_new_session_whose_first_report_is_partial_still_keeps_the_job(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """A complete-looking first report can still lack a separately reported fact
    (here the spool's tag): settle waits a bounded grace for the content to
    converge instead of refusing one report too early."""
    item, source, printer, plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    reconnect(mqtt)
    report_the_spool(mqtt, tray_uuid="")
    asyncio.get_running_loop().call_later(0.05, report_the_spool, mqtt)
    await settle_plan(guard, printer.id, timeout=2, poll=0.01, converge=1)
    guard = await final_guard(guard, printer.id)
    assert printer_manager.start_print(
        printer.id,
        source.filename,
        plate,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )


async def test_a_reconnect_that_never_reports_defers_with_the_settle_timeout(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """A07: silence after the reconnect is a refusal in words, not a hang."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    reconnect(mqtt)
    asked = MagicMock(return_value=True)
    monkeypatch.setattr(printer_manager, "request_status_update", asked)
    with pytest.raises(RoutingDeferred, match="feed_settle_timeout"):
        await settle_plan(guard, printer.id, timeout=0.05, poll=0.01)
    asked.assert_called_once_with(printer.id)


async def test_the_final_guard_alone_never_authorises_an_empty_new_session(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Without the wait the new session has proved nothing yet — still a refusal."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    reconnect(mqtt)
    with pytest.raises(RoutingDeferred, match="feed_state_unavailable"):
        await final_guard(guard, printer.id)


async def test_a_healthy_printer_does_not_wait(db_session, tmp_path, printer_factory, monkeypatch):
    """A08: same generation — no pushall, no sleep."""
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    asked = MagicMock(return_value=True)
    monkeypatch.setattr(printer_manager, "request_status_update", asked)
    await settle_plan(guard, printer.id, timeout=0.01, poll=0.01)
    asked.assert_not_called()


async def test_settle_plan_honours_a_cancel(db_session, tmp_path, printer_factory, monkeypatch):
    """The Cancel button works during the wait."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    reconnect(mqtt)

    class Cancelled(Exception):
        pass

    def raise_if_cancelled():
        raise Cancelled

    with pytest.raises(Cancelled):
        await settle_plan(guard, printer.id, raise_if_cancelled=raise_if_cancelled, timeout=2, poll=0.01)


def empty_the_spool(mqtt):
    mqtt._process_message({"print": {"command": "push_status", "vt_tray": {"id": 254, "tray_type": ""}}})


async def test_a_planned_slot_refilled_within_the_wait_starts(db_session, tmp_path, printer_factory, monkeypatch):
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    empty_the_spool(mqtt)
    asyncio.get_running_loop().call_later(0.05, report_the_spool, mqtt)
    assert await settle_plan(guard, printer.id, timeout=2, poll=0.01, converge=0.05) is False
    assert (await final_guard(guard, printer.id)).plan.mapping == guard.plan.mapping


async def test_the_operator_is_told_the_start_waits_for_a_slot(db_session, tmp_path, printer_factory, monkeypatch):
    """Final review: during the wait the dispatch toast said «Starting print…»."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    told = AsyncMock()
    await settle_plan(guard, printer.id, timeout=0.05, poll=0.01, on_wait=told)  # healthy: nothing to say
    told.assert_not_awaited()
    empty_the_spool(mqtt)
    asyncio.get_running_loop().call_later(0.05, report_the_spool, mqtt)
    await settle_plan(guard, printer.id, timeout=2, poll=0.01, on_wait=told)
    told.assert_awaited_once_with("planned_source_empty")


async def test_a_planned_slot_that_stays_empty_is_refused_without_a_latch(
    db_session, tmp_path, printer_factory, monkeypatch
):
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    empty_the_spool(mqtt)
    with pytest.raises(RoutingDeferred, match="planned_source_empty") as refusal:
        await settle_plan(guard, printer.id, timeout=0.1, poll=0.01, converge=0.05)
    assert refusal.value.revision is None


async def test_a_slot_passing_through_our_own_writes_converges(db_session, tmp_path, printer_factory, monkeypatch):
    """A spool put in at Clear plate: BamDude's own writes show a wrong type for a moment."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    report_the_spool(mqtt, tray_type="PETG")
    asyncio.get_running_loop().call_later(0.03, report_the_spool, mqtt)
    await settle_plan(guard, printer.id, timeout=2, poll=0.01, converge=1)
    assert await final_guard(guard, printer.id)


async def test_a_reconnect_with_a_change_elsewhere_does_not_wait_out_the_grace(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Review Focus 1: a reconnect mid-upload and a spool swap in a slot the job does not use."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    monkeypatch.setattr(printer_manager, "request_status_update", MagicMock(return_value=True))
    reconnect(mqtt)
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "ams": {"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "ABS", "tray_color": "FFFFFFFF"}]}]},
                "vt_tray": {
                    "id": 254,
                    "tray_type": "PLA",
                    "tray_color": "0000FF",
                    "tray_info_idx": "GFA00",
                    "tray_uuid": "THE-SPOOL",
                },
            }
        }
    )
    started = asyncio.get_running_loop().time()
    assert await settle_plan(guard, printer.id, timeout=5, poll=0.01, converge=3) is True
    assert asyncio.get_running_loop().time() - started < 1


async def test_edit_echoing_mapping_keeps_original_pin_evidence(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    added = await committing_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": queue.id,
            "library_file_id": source.id,
            "ams_mapping": [-1, -1, 254],
            "manual_mapping": True,
        },
    )
    assert added.status_code == 200, added.text
    item = await db_session.get(PrintQueueItem, added.json()["id"])
    before = json.loads(item.filament_routing)["physical_pins"]
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_color": "00FF00"}}})
    edited = await committing_client.patch(
        f"/api/v1/queue/{item.id}",
        json={
            "ams_mapping": [-1, -1, 254],
            "manual_mapping": True,
            "manual_start": True,
        },
    )
    assert edited.status_code == 200, edited.text
    await db_session.refresh(item)
    assert json.loads(item.filament_routing)["physical_pins"] == before
    # П4: without a forced colour a recoloured spool still holds the pin.
    assert await preflight_item(db_session, item, printer.id)
    reviewed = await committing_client.patch(
        f"/api/v1/queue/{item.id}",
        json={
            "ams_mapping": [-1, -1, 254],
            "manual_mapping": True,
            "remap_filament": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    await db_session.refresh(item)
    assert json.loads(item.filament_routing)["physical_pins"] != before
    assert await preflight_item(db_session, item, printer.id)


async def test_final_guard_still_refuses_a_swapped_spool_with_the_option_on(
    db_session, tmp_path, printer_factory, monkeypatch
):
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
    with pytest.raises(RoutingDeferred, match="material_mismatch"):
        await final_guard(guard, printer.id)


async def test_final_guard_refuses_when_the_bind_went_to_another_session(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Final review I2: the K bind went to the session it was sent on; a start on
    another one would print with no K selected."""
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    bind_generation = mqtt.state.connection_generation
    reconnect(mqtt)
    report_the_spool(mqtt)
    with pytest.raises(RoutingDeferred, match="printer_reconnected") as refusal:
        await final_guard(guard, printer.id, bind_generation=bind_generation)
    assert refusal.value.revision is None  # never latched


@pytest.mark.parametrize(("pinned", "asked"), [(254, False), (0, True)])
async def test_a_pinned_job_ranks_the_inventory_only_when_a_twin_may_be_needed(
    db_session, tmp_path, printer_factory, monkeypatch, pinned, asked
):
    """Final review: ranking a pinned job cost a Spoolman read per bound slot on
    every preflight; it only matters when the pinned slot is empty."""
    from backend.app.services.filament_preflight import ranked_feed
    from backend.app.services.filament_routing import RoutingPolicy
    from backend.app.services.print_scheduler import scheduler

    _item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    overrides = AsyncMock(return_value={})
    monkeypatch.setattr(scheduler, "_build_inventory_remain_overrides", overrides)
    policy = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": pinned}})
    await ranked_feed(db_session, printer.id, policy, prefer_lowest=True)
    assert overrides.await_count == (1 if asked else 0)


async def test_final_guard_lets_a_new_tag_on_the_same_filament_through(
    db_session, tmp_path, printer_factory, monkeypatch
):
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_uuid": "ANOTHER-SPOOL"}}})
    assert (await final_guard(guard, printer.id)).plan.mapping == guard.plan.mapping


@pytest.mark.parametrize(
    ("change", "reason"), [("material", "material_mismatch"), ("reconnect", "printer_reconnected")]
)
async def test_the_publish_boundary_still_catches_a_real_change_with_the_option_on(
    db_session, tmp_path, printer_factory, monkeypatch, change, reason
):
    item, source, printer, plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    if change == "reconnect":
        mqtt.state.connection_generation += 1
    else:
        mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
    with pytest.raises(RoutingDeferred, match=reason):
        printer_manager.start_print(
            printer.id,
            source.filename,
            plate,
            ams_mapping=guard.plan.mapping,
            use_ams=guard.plan.use_ams,
            routing_guard=guard,
        )
    mqtt._client.publish.assert_not_called()


@pytest.mark.parametrize("tray", [{"tray_color": "00FF00"}, {"tray_uuid": "ANOTHER-SPOOL"}])
async def test_the_publish_boundary_lets_a_new_colour_or_tag_through(
    db_session, tmp_path, printer_factory, monkeypatch, tray
):
    item, source, printer, plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, **tray}}})
    assert printer_manager.start_print(
        printer.id,
        source.filename,
        plate,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )
    mqtt._client.publish.assert_called_once()


async def test_a_block_recorded_the_old_way_no_longer_holds_a_compatible_job(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Nothing clears a stored block: the old one simply stops matching.

    The revision written before the boundary was policy-aware hashed the
    snapshot's own marker, profile ids included, so it can no longer equal what
    this build computes for the same unchanged feed.
    """
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    req = await read_item_requirements(db_session, item)
    stale = fingerprint(
        {
            "source": req.source_identity.revision(),
            "policy": queue_policy(item).fingerprint,
            "snapshot": printer_manager.get_feed_snapshot(printer.id).marker,
        }
    )
    item.filament_routing = json.dumps(
        {**json.loads(item.filament_routing), "runtime": {"reason": "feed_settle_timeout", "blocked_revision": stale}}
    )
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan is not None


async def test_the_same_old_block_still_holds_a_job_that_kept_the_profile(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """The mirror of the test above, and the reason it is not a regression.

    Nothing was migrated and no latch was cleared: with the option OFF
    ``feed_signature`` IS the snapshot's own marker, so the revision an older
    build wrote is byte-for-byte the one this build computes, and the block goes
    on holding. Only jobs that had been told to ignore profiles were let go.
    """
    item, _source, printer, _plate, _mqtt = await a_routed_job(
        db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match=False
    )
    req = await read_item_requirements(db_session, item)
    stale = fingerprint(
        {
            "source": req.source_identity.revision(),
            "policy": queue_policy(item).fingerprint,
            "snapshot": printer_manager.get_feed_snapshot(printer.id).marker,
        }
    )
    item.filament_routing = json.dumps(
        {**json.loads(item.filament_routing), "runtime": {"reason": "feed_settle_timeout", "blocked_revision": stale}}
    )
    await db_session.commit()
    with pytest.raises(RoutingDeferred, match="feed_settle_timeout"):
        await preflight_item(db_session, item, printer.id)


async def test_a_settle_timeout_still_holds_the_job_while_nothing_changes(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """The latch itself is untouched — only the key it is written under changed."""
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    item.filament_routing = json.dumps(
        {
            **json.loads(item.filament_routing),
            "runtime": {"reason": "feed_settle_timeout", "blocked_revision": guard.revision},
        }
    )
    await db_session.commit()
    with pytest.raises(RoutingDeferred, match="feed_settle_timeout"):
        await preflight_item(db_session, item, printer.id)


async def test_a_refusal_for_a_changed_feed_does_not_park_the_job(db_session, tmp_path, printer_factory, monkeypatch):
    """The latch recorded the feed AFTER the change — a state nobody has tried (spec Д3)."""
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    item.filament_routing = json.dumps(
        {
            **json.loads(item.filament_routing),
            "runtime": {"reason": "feed_state_changed", "blocked_revision": guard.revision},
        }
    )
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan is not None


async def test_a_runtime_without_a_reason_does_not_park_the_job(db_session, tmp_path, printer_factory, monkeypatch):
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    item.filament_routing = json.dumps(
        {**json.loads(item.filament_routing), "runtime": {"blocked_revision": guard.revision}}
    )
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan is not None


def ams_report(*loaded, filam_bak=None):
    """AMS 0 with PLA red in the ``loaded`` slots and the others empty."""
    trays = [
        {"id": t, "tray_type": "PLA", "tray_color": "FF0000FF"} if t in loaded else {"id": t, "tray_type": ""}
        for t in range(4)
    ]
    report = {"command": "push_status", "ams": {"ams_exist_bits": "1", "ams": [{"id": 0, "tray": trays}]}}
    if filam_bak is not None:
        report["filam_bak"] = filam_bak
    return {"print": report}


async def a_job_pinned_to_ams_slot_0(db, tmp_path, printer_factory, monkeypatch, *, filam_bak):
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    mqtt._process_message(ams_report(0, 1, filam_bak=filam_bak))
    routing, plate = await prepare_routing(
        db,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"manual_mapping": True, "ams_mapping": [-1, -1, 0]},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db.add(item)
    await db.commit()
    return item, source, printer, plate, mqtt


async def test_a_pinned_slot_that_ran_dry_prints_from_its_twin_end_to_end(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Spec §6 (final review): the dispatcher's own preflight picks the AMS Backup
    twin, and the command published to the printer names it."""
    item, source, printer, plate, mqtt = await a_job_pinned_to_ams_slot_0(
        db_session, tmp_path, printer_factory, monkeypatch, filam_bak=[3]
    )
    mqtt._process_message(ams_report(1, filam_bak=[]))  # slot 0 ran dry; the firmware drops the group
    mqtt.state.ams_auto_switch_filament = True
    guard = await preflight_item(db_session, item, printer.id)
    assert guard.plan.mapping == [-1, -1, 1]
    guard = await final_guard(guard, printer.id)
    assert printer_manager.start_print(
        printer.id,
        source.filename,
        plate,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert 1 in command["ams_mapping"] and 0 not in command["ams_mapping"]


async def test_an_empty_pinned_slot_is_never_parked_and_plans_once_its_group_is_known(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """Spec §6: ``pinned_source_empty`` carries a revision from the dispatcher's
    preflight, yet it must not latch — the job plans as soon as the group is known."""
    from backend.app.services.filament_preflight import revision_for

    item, _source, printer, _plate, mqtt = await a_job_pinned_to_ams_slot_0(
        db_session, tmp_path, printer_factory, monkeypatch, filam_bak=None
    )
    mqtt._process_message(ams_report(1))  # slot 0 dry, no group ever reported
    mqtt.state.ams_auto_switch_filament = True
    with pytest.raises(RoutingDeferred, match="pinned_source_empty"):
        await preflight_item(db_session, item, printer.id)
    mqtt._process_message(ams_report(1, filam_bak=[3]))  # the firmware reports the group
    revision = revision_for(
        await read_item_requirements(db_session, item),
        queue_policy(item),
        printer_manager.get_feed_snapshot(printer.id),
    )
    item.filament_routing = json.dumps(
        {
            **json.loads(item.filament_routing),
            "runtime": {"reason": "pinned_source_empty", "blocked_revision": revision},
        }
    )
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan.mapping == [-1, -1, 1]


@pytest.mark.parametrize("allow_base_material_match", [False, True])
@pytest.mark.parametrize("force_color_match", [False, True])
async def test_retained_partial_stock_assignment_cannot_admit_an_empty_slot(
    db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match, force_color_match
):
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.filament_policy_write import prepare_routing
    from backend.tests.integration.test_ams_unlink_runout_guard import _run_on_ams_change
    from backend.tests.unit.services.test_auto_stock_spool import GROUP, spool

    item, source, printer, _, mqtt = await a_job_pinned_to_ams_slot_0(
        db_session, tmp_path, printer_factory, monkeypatch, filam_bak=None
    )
    printer.ams_policies = {"auto_stock_spool": {"enabled": True, "group": GROUP}}
    partial = await spool(db_session, weight_used=300)
    db_session.add(
        SpoolAssignment(
            printer_id=printer.id,
            spool_id=partial.id,
            ams_id=0,
            tray_id=0,
            fingerprint_type="PLA",
            fingerprint_color="FF0000FF",
        )
    )
    routing, _ = await prepare_routing(
        db_session,
        printer_id=printer.id,
        library_file_id=source.id,
        options={
            "manual_mapping": True,
            "ams_mapping": [-1, -1, 0],
            "allow_base_material_match": allow_base_material_match,
            "force_color_match": force_color_match,
        },
    )
    item.filament_routing = routing
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", session)
    empty = [{"id": 0, "tray": [{"id": 0, "exists": False, "state": 9, "tray_type": "", "tray_color": ""}]}]
    await _run_on_ams_change(printer.id, empty, "IDLE")
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalar_one() == partial.id
    mqtt._process_message(ams_report())
    with pytest.raises(RoutingDeferred, match="pinned_source_empty"):
        await preflight_item(db_session, item, printer.id)
    mqtt._client.publish.assert_not_called()
