"""Synthetic order policy through the real API and captured-source producer."""

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services.queue_add import add_items_to_printer_queue
from backend.app.services.queue_ops import _copy_item_fields

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_order_flag_snapshot_and_clone_do_not_change_printer_or_existing_work(
    committing_client,
    db_session,
    printer_factory,
    raw_gcode_source,
):
    a = (
        await committing_client.post(
            "/api/v1/projects",
            json={
                "name": "Product A order",
                "auto_eject_enabled": True,
                "auto_eject_settings": {"difference_threshold": 2.0, "skip_check": False},
            },
        )
    ).json()
    b = (await committing_client.post("/api/v1/projects", json={"name": "Product B order"})).json()
    assert a["auto_eject_enabled"] is True
    assert b["auto_eject_enabled"] is False
    assert b["auto_eject_settings"] == {"difference_threshold": 1.0, "skip_check": False}
    printer = await printer_factory(model="P1S", require_plate_clear=True, plate_detection_enabled=False)
    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    rows, _ = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(
            queue_id=queue.id,
            library_file_id=raw_gcode_source.id,
            project_id=a["id"],
            quantity=2,
        ),
        None,
    )
    assert len(rows) == 2 and all(r.auto_eject is True and r.queue_source_id is not None for r in rows)
    assert all(r.auto_eject_settings == a["auto_eject_settings"] for r in rows)
    old_id = rows[0].id
    changed = await committing_client.patch(
        f"/api/v1/projects/{a['id']}",
        json={
            "auto_eject_enabled": False,
            "auto_eject_settings": {"difference_threshold": 3.0, "skip_check": True},
            "auto_eject_skip_acknowledged": True,
        },
    )
    assert changed.status_code == 200 and changed.json()["auto_eject_enabled"] is False
    old = await db_session.get(PrintQueueItem, old_id, populate_existing=True)
    assert old.auto_eject is True
    assert _copy_item_fields(old, None, 99).auto_eject is True
    assert old.auto_eject_settings == {"difference_threshold": 2.0, "skip_check": False}
    clone = _copy_item_fields(old, None, 99)
    assert clone.auto_eject_settings == old.auto_eject_settings
    clone.auto_eject_settings["difference_threshold"] = 4
    assert old.auto_eject_settings["difference_threshold"] == 2
    assert printer.require_plate_clear is True and printer.plate_detection_enabled is False
    new, _ = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(
            queue_id=queue.id,
            library_file_id=raw_gcode_source.id,
            project_id=a["id"],
            quantity=1,
        ),
        None,
    )
    assert new[0].auto_eject is False
    assert new[0].auto_eject_settings == {"difference_threshold": 3.0, "skip_check": True}
    null = await committing_client.patch(f"/api/v1/projects/{a['id']}", json={"auto_eject_enabled": None})
    assert null.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        None,
        {"difference_threshold": 0},
        {"difference_threshold": 11},
        {"difference_threshold": "NaN"},
        {"difference_threshold": "Infinity"},
        {"skip_check": True},
        {"unknown": True},
    ],
)
async def test_invalid_or_unacknowledged_policy_is_refused_without_writing(committing_client, settings):
    order = (await committing_client.post("/api/v1/projects", json={"name": "Product B order"})).json()
    response = await committing_client.patch(f"/api/v1/projects/{order['id']}", json={"auto_eject_settings": settings})
    assert response.status_code == 422
    unchanged = (await committing_client.get(f"/api/v1/projects/{order['id']}")).json()
    assert unchanged["auto_eject_settings"] == {"difference_threshold": 1.0, "skip_check": False}


@pytest.mark.asyncio
async def test_acknowledgement_is_request_only_and_check_can_be_restored(committing_client):
    response = await committing_client.post(
        "/api/v1/projects",
        json={
            "name": "Product A order",
            "auto_eject_settings": {"difference_threshold": 10, "skip_check": True},
            "auto_eject_skip_acknowledged": True,
        },
    )
    assert response.status_code in (200, 201)
    order = response.json()
    assert "auto_eject_skip_acknowledged" not in order
    restored = await committing_client.patch(
        f"/api/v1/projects/{order['id']}",
        json={"auto_eject_settings": {"difference_threshold": 1, "skip_check": False}},
    )
    assert restored.status_code == 200 and restored.json()["auto_eject_settings"]["skip_check"] is False
