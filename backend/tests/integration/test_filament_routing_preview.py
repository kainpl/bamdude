import json

import pytest

from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.printer_manager import printer_manager
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


async def test_preview_uses_strict_reader_and_never_exposes_paths(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    response = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={
            "library_file_id": source.id,
            "plate_ids": [0],
            "force_color_match": False,
        },
    )
    assert response.status_code == 200, response.text
    plate = response.json()["plates"][0]
    assert plate["status"] == "ok" and plate["plate_id"] == 15
    assert plate["filaments"][0]["slot_id"] == 3
    group = plate["groups"][0]
    assert group["ams"] == "absent" and group["compatible"] == 1
    assert str(tmp_path) not in json.dumps(response.json())
    mqtt._client.publish.assert_not_called()
    strict = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={
            "library_file_id": source.id,
            "plate_ids": [15],
            "force_color_match": True,
        },
    )
    assert strict.json()["plates"][0]["groups"][0]["compatible"] == 0
    assert strict.json()["plates"][0]["groups"][0]["reasons"][0]["code"] == "color_mismatch"


async def test_printer_preview_checks_actual_manual_source_and_feed_policy(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    body = {"library_file_id": source.id, "targets": [{"printer_id": printer.id, "plate_id": 15}]}
    response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["targets"][0]["mapping"] == [-1, -1, 254]
    response = await committing_client.post(
        "/api/v1/auto-queue/printer-routing-preview", json={**body, "feed_policy": "ams_only"}
    )
    assert response.json()["targets"][0]["status"] == "incompatible"
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
    body["targets"][0].update(manual_mapping=True, ams_mapping=[-1, -1, 254])
    response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json=body)
    assert response.json()["targets"][0]["reason"]["code"] == "material_mismatch"
    mqtt._client.publish.assert_not_called()


async def test_printer_preview_reads_captured_bytes_after_original_is_gone(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    from pathlib import Path

    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    added = await committing_client.post("/api/v1/queue/", json={"queue_id": queue.id, "library_file_id": source.id})
    assert added.status_code == 200, added.text
    Path(source.file_path).unlink()  # only this test's synthetic tmp_path source
    preview = await committing_client.post(
        "/api/v1/auto-queue/printer-routing-preview",
        json={
            "source_queue_item_id": added.json()["id"],
            "targets": [{"printer_id": printer.id, "plate_id": 15}],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["targets"][0]["status"] == "compatible"


async def test_printer_preview_keeps_saved_pin_until_explicit_review(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    # Queue primary keys are not printer IDs.
    queue.id = printer.id + 1000
    await db_session.commit()
    added = await committing_client.post(
        "/api/v1/queue/",
        json={"queue_id": queue.id, "library_file_id": source.id, "manual_mapping": True, "ams_mapping": [-1, -1, 254]},
    )
    assert added.status_code == 200, added.text
    target = {"printer_id": printer.id, "plate_id": 15, "manual_mapping": True, "ams_mapping": [-1, -1, 254]}
    body = {"source_queue_item_id": added.json()["id"], "editing_queue_item": True, "targets": [target]}
    response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json=body)
    assert response.json()["targets"][0]["status"] == "compatible"
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "00FF00"}}})
    response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json=body)
    assert response.json()["targets"][0]["reason"]["code"] == "mapping_review_required"
    copied = await committing_client.post(
        "/api/v1/auto-queue/printer-routing-preview", json={**body, "editing_queue_item": False}
    )
    assert copied.json()["targets"][0]["status"] == "compatible"
    target["remap_filament"] = True
    response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json=body)
    assert response.json()["targets"][0]["status"] == "compatible"
    mqtt._client.publish.assert_not_called()


async def test_printer_preview_checks_full_assignment_overrides_nozzle_and_unknown(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    from dataclasses import replace

    from backend.tests.unit.services.test_filament_routing import feed, snapshot

    source, printer, _, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    write_routing_3mf(
        tmp_path / source.filename,
        {
            1: [
                {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": "1"},
                {"id": 2, "type": "PLA", "color": "#FF0000", "tray_info_idx": "A", "used_g": "1"},
            ]
        },
        settings={"nozzle_diameter": ["0.4"]},
    )
    state = snapshot(feed(0, kind="ams", variant="A"), feed(1, kind="ams", variant="B"), nozzle_diameters={0: (0.4,)})
    monkeypatch.setattr(printer_manager, "get_feed_snapshot", lambda _: state)
    body = {
        "library_file_id": source.id,
        "allow_base_material_match": False,
        "targets": [{"printer_id": printer.id, "plate_id": 1}],
    }

    async def result(**changes):
        response = await committing_client.post("/api/v1/auto-queue/printer-routing-preview", json={**body, **changes})
        assert response.status_code == 200, response.text
        return response.json()["targets"][0]

    assert (await result())["mapping"] == [1, 0]
    assert (await result(filament_overrides=[{"slot_id": 2, "type": "PETG"}]))["reason"]["code"] == "material_mismatch"
    state = replace(state, nozzle_diameters={0: (0.6,)})
    assert (await result())["reason"]["code"] == "nozzle_mismatch"
    state = replace(state, connected=False)
    assert (await result())["status"] == "unknown"


@pytest.mark.parametrize("owns", [False, True])
async def test_printer_preview_obeys_queue_source_ownership(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, owns
):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.user import User

    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    added = await committing_client.post("/api/v1/queue/", json={"queue_id": queue.id, "library_file_id": source.id})
    item = await db_session.get(PrintQueueItem, added.json()["id"])
    group = Group(name="Preview own queue", permissions=["printers:read", "queue:read_own"])
    user = User(username="preview_owner", password_hash="unused", role="user", is_active=True, groups=[group])
    db_session.add_all([group, user])
    await db_session.flush()
    item.created_by_id = user.id if owns else None
    await db_session.commit()
    response = await committing_client.post(
        "/api/v1/auto-queue/printer-routing-preview",
        json={
            "source_queue_item_id": item.id,
            "targets": [{"printer_id": printer.id, "plate_id": 15}],
        },
        headers={"Authorization": f"Bearer {create_access_token(data={'sub': user.username})}"},
    )
    assert response.status_code == (200 if owns else 404), response.text


async def test_preview_matches_the_base_material_by_default_and_can_require_the_exact_preset(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """The channel's own material decides; the family it resolves to is reported, not obeyed.

    The preset id here is one no install but its author's can name, and the tray
    carries a different one. With the option on that is one PETG on both sides;
    with it off the operator asked for that exact preset. Either way the
    response still carries the family the catalogue resolved, because the UI
    shows it.
    """
    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    write_routing_3mf(
        tmp_path / source.filename,
        {15: [{"id": 3, "type": "PETG", "tray_info_idx": "P333PETG", "used_g": "0.0001"}]},
    )
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
    await db_session.commit()
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "ams": {"ams": []},
                "vt_tray": {"id": 254, "tray_type": "PETG", "tray_info_idx": "GFG99"},
            }
        }
    )
    assert printer_manager.get_feed_snapshot(printer.id).sources[0].material == "PETG"

    strict = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={"library_file_id": source.id, "plate_ids": [15], "allow_base_material_match": False},
    )
    default = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={"library_file_id": source.id, "plate_ids": [15]},
    )
    assert strict.status_code == default.status_code == 200
    assert strict.json()["plates"][0]["groups"][0]["reasons"][0]["code"] == "variant_mismatch"
    assert default.json()["plates"][0]["filaments"][0]["filament_type"] == "PETG"
    assert default.json()["plates"][0]["groups"][0]["compatible"] == 1


async def test_preview_missing_plate_is_unavailable_not_fallback(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    result = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={
            "library_file_id": source.id,
            "plate_ids": [1, 15],
        },
    )
    assert result.status_code == 200, result.text
    missing, valid = result.json()["plates"]
    assert missing["status"] == "unavailable" and missing["reason"]["code"] == "plate_not_found"
    assert missing["filaments"] == []
    assert valid["status"] == "ok"


async def test_preview_bounds_plate_list(committing_client):
    result = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={
            "library_file_id": 1,
            "plate_ids": list(range(65)),
        },
    )
    assert result.status_code == 422


async def test_preview_monitoring_failure_keeps_valid_source_queueable(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)

    def unavailable(*args):
        raise RuntimeError("monitor restarting")

    monkeypatch.setattr("backend.app.services.filament_preview.printer_manager.get_feed_snapshot", unavailable)
    response = await committing_client.post("/api/v1/auto-queue/routing-preview", json={"library_file_id": source.id})
    assert response.status_code == 200, response.text
    assert response.json()["advisory_unavailable"]
    assert response.json()["plates"][0]["status"] == "ok"
    queued = await committing_client.post("/api/v1/auto-queue/", json={"library_file_id": source.id})
    assert queued.status_code == 200, queued.text


async def test_compatible_printer_with_pending_work_is_not_ready(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, _, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    queue.pending_count = 2
    await db_session.commit()
    response = await committing_client.post("/api/v1/auto-queue/routing-preview", json={"library_file_id": source.id})
    group = response.json()["plates"][0]["groups"][0]
    assert group["compatible"] == 1 and group["ready"] == 0


@pytest.mark.parametrize(
    "permissions,owns_source,expected",
    [
        (["queue:create", "printers:read", "library:read_own"], True, 200),
        (["queue:create", "printers:read", "library:read_own"], False, 404),
        (["queue:create", "library:read_all"], True, 403),
        (["queue:create", "printers:read"], True, 403),
    ],
)
@pytest.mark.parametrize("specific", [False, True])
async def test_preview_respects_source_ownership_and_printer_read_permission(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, permissions, owns_source, expected, specific
):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.user import User

    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    group = Group(name="Routing preview reader", permissions=permissions)
    user = User(username="routing_reader", password_hash="unused", role="user", is_active=True, groups=[group])
    db_session.add_all([group, user])
    await db_session.flush()
    source.created_by_id = user.id if owns_source else None
    await db_session.commit()
    token = create_access_token(data={"sub": user.username})
    response = await committing_client.post(
        "/api/v1/auto-queue/printer-routing-preview" if specific else "/api/v1/auto-queue/routing-preview",
        json={
            "library_file_id": source.id,
            **({"targets": [{"printer_id": printer.id, "plate_id": 15}]} if specific else {}),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected, response.text
    if expected != 200:
        assert "filaments" not in response.text and str(tmp_path) not in response.text
