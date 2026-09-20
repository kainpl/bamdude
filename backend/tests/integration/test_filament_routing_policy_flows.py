"""Intent survives the actual API create/edit/clone/retry boundaries."""

import json

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.filament_preflight import preflight_item
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


async def test_api_edit_clone_retry_preserves_auto_and_slot_rules(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    created = await committing_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": queue.id,
            "library_file_id": source.id,
            "feed_policy": "auto",
            "force_color_match": False,
            "filament_overrides": [{"slot_id": 3, "color": "#0000FF", "force_color_match": True}],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    iid = body["id"]
    policy = body["filament_routing"]
    assert policy["mode"] == "auto" and policy["force_color_match"] is False
    assert body["plate_id"] == 15
    edited = await committing_client.patch(f"/api/v1/queue/{iid}", json={"manual_start": True})
    assert edited.status_code == 200, edited.text
    assert edited.json()["filament_routing"] == policy
    cloned = await committing_client.post(f"/api/v1/queue/{iid}/clone")
    assert cloned.status_code == 200, cloned.text
    assert cloned.json()["filament_routing"] == policy
    row = await db_session.get(PrintQueueItem, iid)
    row.status = "failed"
    await db_session.commit()
    retried = await committing_client.post(f"/api/v1/queue/{iid}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["filament_routing"] == policy
    await db_session.refresh(row)
    plan = (await preflight_item(db_session, row, printer.id)).plan
    assert plan.mapping == [-1, -1, 254] and plan.use_ams is False


async def test_pinned_edit_to_another_printer_requires_an_explicit_mapping_answer(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    from backend.app.models.printer_queue import PrinterQueue

    source, _, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    second = await printer_factory(model="P1P")
    destination = PrinterQueue(id=second.id, printer_id=second.id)
    db_session.add(destination)
    await db_session.commit()
    created = await committing_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": queue.id,
            "library_file_id": source.id,
            "ams_mapping": [-1, -1, 254],
            "manual_mapping": True,
        },
    )
    assert created.status_code == 200, created.text
    iid = created.json()["id"]
    refused = await committing_client.patch(f"/api/v1/queue/{iid}", json={"queue_id": destination.id})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "mapping_review_required"
    accepted = await committing_client.patch(
        f"/api/v1/queue/{iid}", json={"queue_id": destination.id, "ams_mapping": None}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["filament_routing"]["mode"] == "auto"


async def test_a_mapping_alone_is_not_a_pin_and_a_hand_pin_survives_an_unrelated_edit(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """Who chose the trays is stated, never inferred from the array being there.

    The dialog sends the routing it displays back with every add, so the array
    is present on a job nobody touched the slots of. Only ``manual_mapping``
    separates that from an operator who pointed at a tray — and once one has,
    an edit about something else must not quietly undo it.
    """
    source, _, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    add = {"queue_id": queue.id, "library_file_id": source.id, "ams_mapping": [-1, -1, 254]}

    computed = await committing_client.post("/api/v1/queue/", json=add)
    assert computed.status_code == 200, computed.text
    auto_id = computed.json()["id"]
    assert computed.json()["filament_routing"]["mode"] == "auto"
    assert computed.json()["filament_routing"]["physical_pins"] == {}
    # The row still carries the array — the dispatcher recomputes its plan from it.
    assert computed.json()["ams_mapping"] == [-1, -1, 254]

    resent = await committing_client.patch(f"/api/v1/queue/{auto_id}", json={"ams_mapping": [-1, -1, 254]})
    assert resent.status_code == 200, resent.text
    assert resent.json()["filament_routing"]["mode"] == "auto"

    chosen = await committing_client.post("/api/v1/queue/", json={**add, "manual_mapping": True})
    assert chosen.status_code == 200, chosen.text
    pinned_id = chosen.json()["id"]
    pins = chosen.json()["filament_routing"]["physical_pins"]
    assert chosen.json()["filament_routing"]["mode"] == "pinned" and pins

    kept = await committing_client.patch(f"/api/v1/queue/{pinned_id}", json={"force_color_match": True})
    assert kept.status_code == 200, kept.text
    assert kept.json()["filament_routing"]["mode"] == "pinned"
    assert kept.json()["filament_routing"]["physical_pins"] == pins

    released = await committing_client.patch(f"/api/v1/queue/{pinned_id}", json={"manual_mapping": False})
    assert released.status_code == 200, released.text
    assert released.json()["filament_routing"]["mode"] == "auto"
    assert released.json()["filament_routing"]["physical_pins"] == {}


async def test_auto_intake_refuses_slot_rules_from_a_different_file(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    response = await committing_client.post(
        "/api/v1/auto-queue/",
        json={
            "library_file_id": source.id,
            "filament_overrides": [{"slot_id": 1, "force_color_match": True, "color": "#FF0000"}],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "override_slot_not_used"


async def test_auto_rule_survives_deleted_origin_without_relaxing_model(
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.filament_routing import RoutingDeferred
    from backend.app.services.printer_manager import printer_manager

    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    created = await committing_client.post("/api/v1/auto-queue/", json={"library_file_id": source.id})
    assert created.status_code == 200
    auto = await db_session.get(AutoQueueItem, created.json()["id"])
    queued = await AutoQueueScheduler()._assign(db_session, auto, printer)
    queued.source_auto_item_id = None
    await db_session.delete(auto)
    await db_session.commit()
    assert (await preflight_item(db_session, queued, printer.id)).plan.mapping == [-1, -1, 254]
    mqtt.model = "P1S"
    monkeypatch.setitem(printer_manager._models, printer.id, "P1S")
    with pytest.raises(RoutingDeferred, match="model_mismatch"):
        await preflight_item(db_session, queued, printer.id)


async def test_pinned_queue_intake_refuses_file_local_rules_for_an_unused_slot(
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    source, _, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    response = await committing_client.post(
        "/api/v1/queue/",
        json={
            "library_file_id": source.id,
            "queue_id": queue.id,
            "filament_overrides": [{"slot_id": 1, "force_color_match": True}],
        },
    )
    assert response.status_code == 422 and response.json()["detail"]["code"] == "override_slot_not_used"
