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


async def test_preview_uses_profile_family_type_by_default_and_can_require_the_exact_preset(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    write_routing_3mf(
        tmp_path / source.filename,
        {15: [{"id": 3, "type": "333Print PETG", "tray_info_idx": "P333PETG", "used_g": "0.0001"}]},
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
        {"print": {"command": "push_status", "ams": {"ams": []}, "vt_tray": {"id": 254, "tray_type": "PETG"}}}
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
    assert strict.json()["plates"][0]["groups"][0]["reasons"][0]["code"] == "material_mismatch"
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
async def test_preview_respects_source_ownership_and_printer_read_permission(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, permissions, owns_source, expected
):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.user import User

    source, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    group = Group(name="Routing preview reader", permissions=permissions)
    user = User(username="routing_reader", password_hash="unused", role="user", is_active=True, groups=[group])
    db_session.add_all([group, user])
    await db_session.flush()
    source.created_by_id = user.id if owns_source else None
    await db_session.commit()
    token = create_access_token(data={"sub": user.username})
    response = await committing_client.post(
        "/api/v1/auto-queue/routing-preview",
        json={"library_file_id": source.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected, response.text
    if expected != 200:
        assert "filaments" not in response.text and str(tmp_path) not in response.text
