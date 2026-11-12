"""G01 A: read authority cannot repair, mutate or describe a hidden resource."""

import io
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app.api.routes.library import get_library_files_dir, save_3mf_bytes_to_library, store_library_upload
from backend.app.models.ams_history import AMSSensorHistory
from backend.app.models.api_key import APIKey
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.printer_sensor_history import PrinterSensorHistory
from backend.app.models.project import Project
from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services.library_ingest import LibraryReadAccess, find_reusable_row
from backend.app.services.library_preview_jobs import Authority
from backend.tests.auth_helpers import mint_test_authority
from backend.tests.integration.test_library_dedup import _3mf, isolated_archive_dir  # noqa: F401
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("present", [True, False])
async def test_hidden_dedupe_is_refused_before_even_probing_or_repairing_bytes(db_session, monkeypatch, present):
    row = LibraryFile(
        filename="private-canary.3mf",
        file_path="private-canary.3mf",
        file_hash="same",
        created_by_id=7,
        file_type="3mf",
        file_size=1,
    )
    db_session.add(row)
    await db_session.commit()
    presence = Mock(return_value=present)
    monkeypatch.setattr("backend.app.services.library_ingest._file_present", presence)

    with pytest.raises(HTTPException) as refusal:
        await find_reusable_row(db_session, content_hash="same", access=LibraryReadAccess("own", 8))

    assert (refusal.value.status_code, refusal.value.detail) == (400, {"code": "source_unavailable"})
    presence.assert_not_called()
    assert row.file_path == "private-canary.3mf"
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1


@pytest.mark.parametrize("writer", ["upload", "source-url", "hash"])
async def test_shared_ingest_refuses_hidden_match_without_row_or_file_side_effects(
    db_session, isolated_archive_dir, writer
):
    owner = await _user(db_session, "private-owner", ["library:read_all", "library:upload"])
    owner_id = owner.id
    viewer = await _user(db_session, "own-uploader", ["library:read_own", "library:upload"])
    content = _3mf()
    original = await save_3mf_bytes_to_library(
        db_session,
        content=content,
        filename="secret-canary.3mf",
        created_by_id=owner.id,
        source_url="https://example.invalid/private-source",
    )
    before = {p for p in get_library_files_dir().rglob("*") if p.is_file()}
    kwargs = {
        "authority": Authority.user(viewer.id, capture=await mint_test_authority(db_session, username=viewer.username)),
        "created_by_id": viewer.id,
    }
    with pytest.raises(HTTPException) as refusal:
        if writer == "upload":
            await store_library_upload(db_session, filename="mine.3mf", content=content, target_folder=None, **kwargs)
        else:
            await save_3mf_bytes_to_library(
                db_session,
                filename="mine.3mf",
                content=content,
                source_url="https://example.invalid/private-source" if writer == "source-url" else None,
                **kwargs,
            )
    assert refusal.value.detail == {"code": "source_unavailable"}
    assert {p for p in get_library_files_dir().rglob("*") if p.is_file()} == before
    await db_session.refresh(original.file)
    assert original.file.filename == "secret-canary.3mf"
    assert original.file.created_by_id == owner_id
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1


@pytest.mark.parametrize("credential", ["jwt", "header", "bearer"])
async def test_http_upload_cannot_disclose_hidden_dedupe_receipt(
    async_client, db_session, isolated_archive_dir, credential
):
    content = _3mf()
    hidden = await save_3mf_bytes_to_library(db_session, content=content, filename="hidden-name-canary.3mf")
    viewer = await _user(db_session, "upload-only", ["library:upload", "library:read_own"])
    if credential == "jwt":
        headers = await _jwt(viewer.username)
    else:
        key = await _key(db_session, viewer, can_manage_library=True)
        headers = {"X-API-Key": key} if credential == "header" else {"Authorization": f"Bearer {key}"}
    response = await async_client.post(
        "/api/v1/library/files", files={"file": ("mine.3mf", content, "application/octet-stream")}, headers=headers
    )
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": {"code": "source_unavailable"}}
    assert "hidden-name-canary" not in response.text
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1
    assert (await db_session.get(LibraryFile, hidden.file.id)).created_by_id is None


async def test_zip_partial_import_reports_neutral_refusal_and_keeps_visible_success(
    async_client, db_session, isolated_archive_dir
):
    viewer = await _user(db_session, "zip-uploader", ["library:upload", "library:read_own"])
    hidden_bytes = _3mf(b"<private/>")
    await save_3mf_bytes_to_library(db_session, content=hidden_bytes, filename="hidden.3mf")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("denied.3mf", hidden_bytes)
        archive.writestr("allowed.3mf", _3mf(b"<public/>"))
    response = await async_client.post(
        "/api/v1/library/files/extract-zip?preserve_structure=false",
        files={"file": ("models.zip", buffer.getvalue(), "application/zip")},
        headers=await _jwt(viewer.username),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extracted"] == 1
    assert body["errors"] == [{"filename": "denied.3mf", "error": "source_unavailable"}]
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 2


@pytest.mark.parametrize(
    "route,permission,model",
    [
        ("ams-history", "ams_history:read", AMSSensorHistory),
        ("printer-sensor-history", "printer_sensor_history:read", PrinterSensorHistory),
    ],
)
async def test_history_delete_requires_printer_writer_and_preserves_newer_rows(
    async_client, db_session, printer_factory, route, permission, model
):
    printer = await printer_factory()
    reader = await _user(db_session, "history-reader", [permission])
    writer = await _user(db_session, "history-writer", ["printers:update"])
    extra = {"ams_id": 0} if model is AMSSensorHistory else {"sensor_kind": "chamber"}
    old = model(printer_id=printer.id, recorded_at=datetime.now(timezone.utc) - timedelta(days=60), **extra)
    recent = model(printer_id=printer.id, recorded_at=datetime.now(timezone.utc), **extra)
    db_session.add_all([old, recent])
    await db_session.commit()
    denied = await async_client.delete(f"/api/v1/{route}/{printer.id}?days=30", headers=await _jwt(reader.username))
    assert denied.status_code == 403, denied.text
    assert await db_session.scalar(select(func.count()).select_from(model)) == 2
    allowed = await async_client.delete(f"/api/v1/{route}/{printer.id}?days=30", headers=await _jwt(writer.username))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["deleted"] == 1
    assert await db_session.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "calibrate"),
        ("DELETE", "calibrate"),
        ("PUT", "references/0?label=new"),
        ("DELETE", "references/0"),
    ],
)
async def test_camera_viewer_cannot_modify_plate_references(async_client, db_session, monkeypatch, method, path):
    viewer = await _user(db_session, "camera-reader", ["camera:view"])
    detector = Mock()
    monkeypatch.setattr("backend.app.services.plate_detection.PlateDetector", detector)
    response = await async_client.request(
        method, f"/api/v1/printers/123/camera/plate-detection/{path}", headers=await _jwt(viewer.username)
    )
    assert response.status_code == 403, response.text
    detector.assert_not_called()


async def test_reference_writer_updates_and_deletes_through_existing_detector(
    async_client, db_session, printer_factory, monkeypatch
):
    writer = await _user(db_session, "camera-writer", ["printers:update"])
    printer = await printer_factory()
    detector = Mock()
    detector.update_reference_label.return_value = True
    detector.delete_reference.return_value = True
    monkeypatch.setattr("backend.app.services.plate_detection.PlateDetector", lambda: detector)
    monkeypatch.setattr("backend.app.services.plate_detection.is_plate_detection_available", lambda: True)
    path = f"/api/v1/printers/{printer.id}/camera/plate-detection/references/0"
    changed = await async_client.put(path + "?label=Allowed", headers=await _jwt(writer.username))
    removed = await async_client.delete(path, headers=await _jwt(writer.username))
    assert changed.status_code == removed.status_code == 200
    detector.update_reference_label.assert_called_once_with(printer.id, 0, "Allowed")
    detector.delete_reference.assert_called_once_with(printer.id, 0)


async def test_debug_stop_is_a_write_and_discovery_reader_cannot_start_scans(async_client, db_session, monkeypatch):
    reader = await _user(db_session, "settings-reader", ["settings:read", "printers:read"])
    setter = AsyncMock()
    scan = AsyncMock()
    monkeypatch.setattr("backend.app.api.routes.bug_report._set_debug_setting", setter)
    monkeypatch.setattr("backend.app.api.routes.discovery.discovery_service.start", scan)
    stopped = await async_client.post("/api/v1/bug-report/stop-logging", headers=await _jwt(reader.username))
    discovery = await async_client.post("/api/v1/discovery/start", headers=await _jwt(reader.username))
    assert stopped.status_code == discovery.status_code == 403
    setter.assert_not_called()
    scan.assert_not_called()


async def test_key_patch_null_empty_omitted_and_printer_delete_do_not_expand_scope(
    async_client, db_session, printer_factory
):
    first, second = await printer_factory(), await printer_factory()
    virtual = VirtualPrinter(name="Preserved VP", enabled=False, mode="proxy", target_printer_id=first.id)
    db_session.add(virtual)
    await db_session.commit()
    created = await async_client.post("/api/v1/api-keys/", json={"name": "restricted", "printer_ids": [first.id]})
    key_id = created.json()["id"]
    other_keys = {}
    for name, scope in (("all", None), ("none", []), ("both", [first.id, second.id])):
        result = await async_client.post("/api/v1/api-keys/", json={"name": name, "printer_ids": scope})
        assert result.status_code == 200, result.text
        other_keys[name] = result.json()["id"]
    path = f"/api/v1/api-keys/{key_id}"
    assert (await async_client.patch(path, json={"name": "unchanged scope"})).json()["printer_ids"] == [first.id]
    assert (await async_client.patch(path, json={"printer_ids": []})).json()["printer_ids"] == []
    assert (await async_client.patch(path, json={"printer_ids": None})).json()["printer_ids"] is None
    assert (await async_client.patch(path, json={"printer_ids": [first.id]})).json()["printer_ids"] == [first.id]
    deleted = await async_client.delete(f"/api/v1/printers/{first.id}")
    assert deleted.status_code == 200, deleted.text
    row = await db_session.get(APIKey, key_id, populate_existing=True)
    assert row.printer_ids == []
    assert (await db_session.get(APIKey, other_keys["all"], populate_existing=True)).printer_ids is None
    assert (await db_session.get(APIKey, other_keys["none"], populate_existing=True)).printer_ids == []
    assert (await db_session.get(APIKey, other_keys["both"], populate_existing=True)).printer_ids == [second.id]
    replacement = await printer_factory(id=first.id, name="New physical printer")
    reuse = await async_client.get(
        f"/api/v1/printers/{replacement.id}/status", headers={"X-API-Key": created.json()["key"]}
    )
    assert reuse.status_code == 403
    preserved = await db_session.get(VirtualPrinter, virtual.id, populate_existing=True)
    assert preserved is not None and preserved.name == "Preserved VP" and preserved.target_printer_id is None
    denied = await async_client.get(
        f"/api/v1/printers/{second.id}/status", headers={"X-API-Key": created.json()["key"]}
    )
    assert denied.status_code == 403


async def test_workshop_preserves_minimal_print_summary_and_metrics_but_masks_archive_details(
    async_client, db_session, printer_factory
):
    reader = await _user(db_session, "order-reader", ["orders:read"])
    printer = await printer_factory()
    project = Project(name="Visible order")
    db_session.add(project)
    await db_session.flush()
    archive = PrintArchive(
        project_id=project.id,
        printer_id=printer.id,
        filename="private-name-canary.3mf",
        print_name="private-title-canary",
        file_path="private-path-canary",
        file_size=1,
        failure_reason="private-failure-canary",
        status="failed",
        print_time_seconds=3600,
        filament_used_grams=5,
        quantity=3,
    )
    db_session.add(archive)
    await db_session.commit()
    listing = await async_client.get(f"/api/v1/projects/{project.id}/archives", headers=await _jwt(reader.username))
    assert listing.status_code == 200, listing.text
    row = listing.json()[0]
    assert (row["id"], row["quantity"], row["status"], row["restricted"]) == (archive.id, 3, "failed", True)
    timeline = await async_client.get(f"/api/v1/projects/{project.id}/timeline", headers=await _jwt(reader.username))
    assert timeline.status_code == 200, timeline.text
    failed = next(event for event in timeline.json() if event["event_type"] == "print_failed")
    assert failed["description"] is None
    assert failed["metadata"]["print_time_hours"] == 1
    assert failed["metadata"]["filament_grams"] == 5
    assert "private-" not in listing.text + timeline.text


async def test_key_printer_ceiling_is_applied_before_workshop_archive_page_limit(
    async_client, db_session, printer_factory
):
    owner = await _user(db_session, "scoped-orders", ["orders:read", "archives:read_all"])
    allowed, hidden = await printer_factory(), await printer_factory()
    project = Project(name="Scope order")
    db_session.add(project)
    await db_session.flush()
    for printer in (allowed, hidden):
        db_session.add(
            PrintArchive(
                project_id=project.id,
                printer_id=printer.id,
                filename=printer.name,
                status="completed",
                file_path="",
                file_size=0,
            )
        )
    await db_session.commit()
    key = await _key(
        db_session,
        owner,
        can_read_status=True,
        can_manage_projects=True,
        can_manage_archives=True,
        printer_ids=[allowed.id],
    )
    response = await async_client.get(f"/api/v1/projects/{project.id}/archives?limit=1", headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    assert [item["printer_id"] for item in response.json()] == [allowed.id]


async def test_timeline_scopes_printer_foreign_key_before_queue_limit(async_client, db_session, printer_factory):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    owner = await _user(db_session, "timeline-scoped", ["orders:read"])
    allowed = await printer_factory(id=100)
    hidden = await printer_factory(id=200)
    project = Project(name="Timeline scope")
    db_session.add(project)
    await db_session.flush()
    # Deliberately crossed IDs: a queue's primary key is not its printer ID.
    visible_queue = PrinterQueue(id=200, printer_id=allowed.id)
    hidden_queue = PrinterQueue(id=100, printer_id=hidden.id)
    db_session.add_all([visible_queue, hidden_queue])
    await db_session.flush()
    visible = PrintQueueItem(queue_id=200, project_id=project.id, status="pending", position=0)
    invisible = PrintQueueItem(queue_id=100, project_id=project.id, status="pending", position=0)
    db_session.add_all([visible, invisible])
    await db_session.commit()
    key = await _key(db_session, owner, can_read_status=True, can_manage_projects=True, printer_ids=[allowed.id])
    response = await async_client.get(f"/api/v1/projects/{project.id}/timeline?limit=1", headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    assert [row["metadata"]["queue_item_id"] for row in response.json()] == [visible.id]
