"""Synthetic files, real intake/distributor/preflight/MQTT serialization; no printer I/O."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

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
from backend.app.services.filament_preflight import final_guard, preflight_item, settle_feed
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
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
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
    nozzle_ids = (1, 16) if normalize_model_name(model) == "H2C" else (0, 1)
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


async def test_the_same_retag_still_stops_the_job_when_the_option_is_off(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """With the option off the operator asked for that exact profile, here too."""
    item, _source, printer, _plate, mqtt = await a_routed_job(
        db_session, tmp_path, printer_factory, monkeypatch, allow_base_material_match=False
    )
    guard = await preflight_item(db_session, item, printer.id)
    retag(mqtt)
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        await final_guard(guard, printer.id)


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
    await settle_feed(guard, printer.id, timeout=2, poll=0.01)
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
    report_the_spool(mqtt, tray_uuid="ANOTHER-SPOOL")
    await settle_feed(guard, printer.id, timeout=2, poll=0.01)
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        await final_guard(guard, printer.id)
    mqtt._client.publish.assert_not_called()


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
        await settle_feed(guard, printer.id, timeout=0.05, poll=0.01)
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
    await settle_feed(guard, printer.id, timeout=0.01, poll=0.01)
    asked.assert_not_called()


async def test_settle_feed_honours_a_cancel(db_session, tmp_path, printer_factory, monkeypatch):
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
        await settle_feed(guard, printer.id, raise_if_cancelled=raise_if_cancelled, timeout=2, poll=0.01)


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
    with pytest.raises(RoutingDeferred, match="mapping_review_required"):
        await preflight_item(db_session, item, printer.id)
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


@pytest.mark.parametrize(
    ("change", "reason"),
    [({"tray_type": "PETG"}, "material_mismatch"), ({"tray_uuid": "ANOTHER-SPOOL"}, "feed_state_changed")],
)
async def test_final_guard_still_refuses_a_swapped_spool_with_the_option_on(
    db_session, tmp_path, printer_factory, monkeypatch, change, reason
):
    item, _source, printer, _plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, **change}}})
    with pytest.raises(RoutingDeferred, match=reason):
        await final_guard(guard, printer.id)


@pytest.mark.parametrize("change", ["material", "colour", "identity", "reconnect"])
async def test_the_publish_boundary_still_catches_a_real_change_with_the_option_on(
    db_session, tmp_path, printer_factory, monkeypatch, change
):
    item, source, printer, plate, mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    if change == "reconnect":
        mqtt.state.connection_generation += 1
    else:
        tray = {
            "material": {"tray_type": "PETG"},
            "colour": {"tray_color": "00FF00"},
            "identity": {"tray_uuid": "ANOTHER-SPOOL"},
        }[change]
        mqtt._process_message({"print": {"vt_tray": {"id": 254, **tray}}})
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        printer_manager.start_print(
            printer.id,
            source.filename,
            plate,
            ams_mapping=guard.plan.mapping,
            use_ams=guard.plan.use_ams,
            routing_guard=guard,
        )
    mqtt._client.publish.assert_not_called()


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
        {**json.loads(item.filament_routing), "runtime": {"reason": "feed_state_changed", "blocked_revision": stale}}
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
        {**json.loads(item.filament_routing), "runtime": {"reason": "feed_state_changed", "blocked_revision": stale}}
    )
    await db_session.commit()
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        await preflight_item(db_session, item, printer.id)


async def test_a_block_recorded_the_new_way_still_holds_while_nothing_changes(
    db_session, tmp_path, printer_factory, monkeypatch
):
    """The latch itself is untouched — only the key it is written under changed."""
    item, _source, printer, _plate, _mqtt = await a_routed_job(db_session, tmp_path, printer_factory, monkeypatch)
    guard = await preflight_item(db_session, item, printer.id)
    item.filament_routing = json.dumps(
        {
            **json.loads(item.filament_routing),
            "runtime": {"reason": "feed_state_changed", "blocked_revision": guard.revision},
        }
    )
    await db_session.commit()
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        await preflight_item(db_session, item, printer.id)
